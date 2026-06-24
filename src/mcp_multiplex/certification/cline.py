"""Cline real-client certification harness."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from http.client import HTTPResponse
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from mcp_multiplex.adapters import parse_cline_config
from mcp_multiplex.apply import apply_plan, rollback_backup
from mcp_multiplex.approvals import ApprovalStore
from mcp_multiplex.catalog import CatalogStore, match_observed_entry
from mcp_multiplex.certification.codex import CertificationError
from mcp_multiplex.daemon import MCP_SESSION_HEADER, build_server
from mcp_multiplex.install import install_cline_control_plane
from mcp_multiplex.observability import EventStore, ObservedEntryStore, classify_observed_entries
from mcp_multiplex.planning import generate_known_direct_rewrite_plan
from mcp_multiplex.schemas import CatalogEntry, RemediationPlan
from mcp_multiplex.storage import connect

HUB_BASE_URL = "http://127.0.0.1:30000"
CLINE_AGENT_ID = "agent_cline_user_default"
CREATED_AT = "2026-06-21T00:00:00Z"


@dataclass(frozen=True)
class CertificationStep:
    name: str
    ok: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ClineCertificationResult:
    ok: bool
    work_dir: str
    cline_version: str
    plan_id: str
    approval_id: str
    backup_id: str
    steps: list[CertificationStep]
    config_before: str
    config_after: str
    config_after_rollback: str
    runtime_events: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "MCPMultiplexClineCertification",
            "ok": self.ok,
            "work_dir": self.work_dir,
            "cline_version": self.cline_version,
            "plan_id": self.plan_id,
            "approval_id": self.approval_id,
            "backup_id": self.backup_id,
            "steps": [step.to_dict() for step in self.steps],
            "config_before": self.config_before,
            "config_after": self.config_after,
            "config_after_rollback": self.config_after_rollback,
            "runtime_events": self.runtime_events,
        }

    def transcript(self) -> str:
        lines = [
            "# TASK-041 Cline Certification",
            "",
            f"Result: {'PASS' if self.ok else 'FAIL'}",
            f"Work dir: `{self.work_dir}`",
            f"Cline CLI version: `{self.cline_version}`",
            f"Plan: `{self.plan_id}`",
            f"Approval: `{self.approval_id}`",
            f"Backup: `{self.backup_id}`",
            "",
            "## Steps",
            "",
        ]
        for step in self.steps:
            lines.append(f"- [{'x' if step.ok else ' '}] {step.name}: {step.detail}")
        lines.extend(
            [
                "",
                "## Config Before",
                "",
                "```json",
                self.config_before.rstrip(),
                "```",
                "",
                "## Config After Apply",
                "",
                "```json",
                self.config_after.rstrip(),
                "```",
                "",
                "## Config After Rollback",
                "",
                "```json",
                self.config_after_rollback.rstrip(),
                "```",
                "",
                "## Runtime Events",
                "",
                "```json",
                json.dumps(self.runtime_events, indent=2, sort_keys=True),
                "```",
                "",
                "## Review Gate",
                "",
                "Final human approval is still required before upgrading Cline to certified.",
            ]
        )
        return "\n".join(lines) + "\n"


def run_cline_certification(
    *,
    work_dir: Path | None = None,
    cline_bin: str = "cline",
    port: int = 30000,
) -> ClineCertificationResult:
    if port != 30000:
        raise CertificationError("Cline certification must use Hub data-plane port 30000")
    resolved_cline = shutil.which(cline_bin)
    if resolved_cline is None:
        raise CertificationError(f"Cline CLI not found: {cline_bin}")
    root = _work_dir(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    home = root / "home"
    data_dir = root / "data"
    config_dir = root / "config"
    settings_dir = config_dir / "data/settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    config_path = settings_dir / "cline_mcp_settings.json"
    connection = connect(root / "multiplex.db")
    steps: list[CertificationStep] = []

    config_path.write_text(_direct_cline_config(), encoding="utf-8")
    control_install = install_cline_control_plane(
        connection,
        home=home,
        config_path=config_path,
        backup_dir=root / "install-backups",
        actor="cline_certification",
    )
    if control_install.token is None:
        raise CertificationError("Cline control-plane installer did not issue an auth token")
    control_token = control_install.token
    config_before = config_path.read_text(encoding="utf-8")
    catalog_entry = CatalogEntry.from_dict(_fake_context7_catalog_payload())
    CatalogStore(connection).upsert(catalog_entry)
    steps.append(
        CertificationStep(
            "install_mcp_hub_and_direct_fixture",
            True,
            "installed authenticated mcp_hub with the Cline installer and direct context7 fixture",
            {
                "config_path": str(config_path),
                "install_backup_id": control_install.backup.backup_id
                if control_install.backup is not None
                else None,
                "helper_backup_id": control_install.helper_backup.backup_id
                if control_install.helper_backup is not None
                else None,
                "token_ref": control_token.token_ref,
            },
        )
    )

    parsed_before = parse_cline_config(config_path, agent_id=CLINE_AGENT_ID)
    ObservedEntryStore(connection).upsert_many(parsed_before.observed_entries)
    classifications = classify_observed_entries(parsed_before.observed_entries)
    direct = next(item for item in classifications if item.observed_entry.mount_name == "context7")
    if direct.classification != "active_direct_bypass":
        raise CertificationError("direct context7 fixture did not produce active_direct_bypass")
    steps.append(
        CertificationStep(
            "detect_drift",
            True,
            "Cline direct context7 was detected as an active direct bypass",
            {"classification": direct.classification},
        )
    )

    plan = generate_known_direct_rewrite_plan(
        direct.observed_entry,
        catalog_entry,
        match_observed_entry(direct.observed_entry, [catalog_entry]),
        created_at=CREATED_AT,
    )
    _insert_plan(connection, plan)
    approval = ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)
    approval = ApprovalStore(connection).approve(
        approval.approval_id,
        actor="cline_certification",
        channel="cli",
        decided_at=CREATED_AT,
        comment="TASK-041 disposable certification approval",
    )
    apply_result = apply_plan(
        connection,
        plan.plan_id,
        actor="cline_certification",
        backup_dir=root / "backups",
    )
    config_after = config_path.read_text(encoding="utf-8")
    rewritten = json.loads(config_after)
    context7 = rewritten["mcpServers"]["context7"]
    if context7.get("transport") != {
        "type": "streamableHttp",
        "url": f"{HUB_BASE_URL}/servers/context7/mcp",
    }:
        raise CertificationError("Cline config was not rewritten to native Hub remote URL")
    if context7.get("autoApprove") != ["ping"] or context7.get("disabled") is not False:
        raise CertificationError("Cline rewrite did not preserve disabled/autoApprove fields")
    steps.append(
        CertificationStep(
            "rewrite_through_hub",
            True,
            "approved plan rewrote context7 through the Hub and preserved Cline fields",
            {"plan_id": plan.plan_id, "backup_id": apply_result.backup.backup_id},
        )
    )

    cline_version = _run_cline(
        [resolved_cline, "--version"],
        home=home,
        control_token=control_token.token,
    ).strip()
    cline_config = _run_cline(
        [
            resolved_cline,
            "--data-dir",
            str(data_dir),
            "--config",
            str(config_dir),
            "--json",
            "config",
        ],
        home=home,
        control_token=control_token.token,
    )
    cline_state = json.loads(cline_config)
    _assert_cline_mcp(cline_state, "mcp_hub", enabled=True)
    _assert_cline_mcp(cline_state, "context7", enabled=True)
    steps.append(
        CertificationStep(
            "verify_cline_sees_hub_routed_mcp",
            True,
            "real Cline CLI reported mcp_hub and context7 from disposable Hub-routed settings",
            {"mcp": cline_state.get("mcp", [])},
        )
    )

    server, thread = _start_runtime_server(connection, port=port)
    try:
        session_id, _ = _post_mcp(
            port,
            "/servers/context7/mcp",
            {"jsonrpc": "2.0", "id": "init-cert", "method": "initialize"},
        )
        _, tool_body = _post_mcp(
            port,
            "/servers/context7/mcp",
            {
                "jsonrpc": "2.0",
                "id": "call-cert",
                "method": "tools/call",
                "params": {"name": "ping", "arguments": {}},
            },
            session_id=session_id,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    if tool_body.get("result") != {
        "content": [{"text": "pong", "type": "text"}],
        "isError": False,
    }:
        raise CertificationError("Hub-routed safe tool call did not return pong")
    runtime_events = [
        event.to_dict()
        for event in EventStore(connection).query()
        if event.event.event_type.startswith("runtime.")
    ]
    if not runtime_events:
        raise CertificationError("runtime events were not emitted")
    steps.append(
        CertificationStep(
            "safe_tool_call_and_runtime_events",
            True,
            "Hub-routed runtime initialized and returned ping/pong with runtime events",
            {"runtime_event_types": [event["event_type"] for event in runtime_events]},
        )
    )

    rollback = rollback_backup(
        connection,
        apply_result.backup.backup_id,
        actor="cline_certification",
    )
    config_after_rollback = config_path.read_text(encoding="utf-8")
    if config_after_rollback != config_before:
        raise CertificationError("rollback did not restore exact Cline settings bytes")
    steps.append(
        CertificationStep(
            "rollback",
            True,
            "rollback restored exact pre-image bytes",
            {"restored_hash": rollback.restored_hash},
        )
    )
    return ClineCertificationResult(
        ok=True,
        work_dir=str(root),
        cline_version=cline_version,
        plan_id=plan.plan_id,
        approval_id=approval.approval_id,
        backup_id=apply_result.backup.backup_id,
        steps=steps,
        config_before=config_before,
        config_after=config_after,
        config_after_rollback=config_after_rollback,
        runtime_events=runtime_events,
    )


def _work_dir(work_dir: Path | None) -> Path:
    if work_dir is not None:
        return work_dir.expanduser().resolve()
    return Path(tempfile.mkdtemp(prefix="mcp-multiplex-cline-cert-")).resolve()


def _direct_cline_config() -> str:
    payload = {
        "mcpServers": {
            "context7": {
                "command": sys.executable,
                "args": [str(_fake_backend_path())],
                "disabled": False,
                "autoApprove": ["ping"],
            },
        }
    }
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _fake_backend_path() -> Path:
    return Path(__file__).resolve().parents[3] / "tests/fixtures/runtime/fake_stdio_mcp.py"


def _fake_context7_catalog_payload() -> dict[str, Any]:
    fake_backend = _fake_backend_path()
    return {
        "schema_version": 1,
        "catalog_id": "srv_context7",
        "name": "context7",
        "canonical_name": "upstash.context7",
        "family_id": "context7",
        "variant_name": "certification_fake_stdio",
        "display_label": "Context7 Certification Fixture",
        "aliases": ["context7-mcp", "@upstash/context7-mcp"],
        "review_state": "approved",
        "lifecycle_state": "enabled",
        "risk_tier": "normal",
        "provenance": [{"source": "certification_fixture", "metadata": {}}],
        "transport": {
            "frontend": "streamable_http",
            "hub_path": "/servers/context7/mcp",
            "backend": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(fake_backend)],
                "cwd_policy": "none",
                "env": [],
                "url": None,
            },
        },
        "runtime": {
            "shareability": "global",
            "concurrency": "concurrent_readonly",
            "idle_timeout_sec": 600,
            "health_check": "tools_list",
        },
        "credentials": [],
        "active_set": {"eligible_profiles": ["coding-default"], "default_enabled": False},
    }


def _insert_plan(connection: sqlite3.Connection, plan: RemediationPlan) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO remediation_plans (
              plan_id,
              schema_version,
              plan_type,
              status,
              agent_id,
              target_path,
              observed_entry_id,
              catalog_id,
              policy_json,
              diff_format,
              diff_text,
              expected_preimage_hash,
              rollback_strategy,
              risk_json,
              created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.plan_id,
                plan.schema_version,
                plan.plan_type,
                plan.status,
                plan.agent_id,
                plan.target_path,
                plan.observed_entry_id,
                plan.catalog_id,
                json.dumps(plan.policy, sort_keys=True),
                plan.diff.format,
                plan.diff.text,
                plan.expected_preimage_hash,
                plan.rollback_strategy,
                json.dumps(plan.risk, sort_keys=True),
                plan.created_at,
            ),
        )


def _run_cline(
    command: list[str],
    *,
    home: Path,
    control_token: str | None = None,
) -> str:
    env = {**os.environ, "HOME": str(home)}
    if control_token is not None:
        env["MCP_MULTIPLEX_CONTROL_TOKEN"] = control_token
    result = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )
    if result.returncode != 0:
        raise CertificationError(
            f"Cline command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result.stdout + result.stderr


def _assert_cline_mcp(cline_state: dict[str, Any], server_name: str, *, enabled: bool) -> None:
    mcp_entries = cline_state.get("mcp")
    if not isinstance(mcp_entries, list):
        raise CertificationError("Cline config output did not include an mcp list")
    for entry in mcp_entries:
        if isinstance(entry, dict) and entry.get("id") == server_name:
            if entry.get("enabled") is not enabled:
                raise CertificationError(
                    f"Cline reported unexpected enabled state for {server_name}"
                )
            return
    raise CertificationError(f"Cline did not report MCP entry: {server_name}")


def _start_runtime_server(
    connection: sqlite3.Connection,
    *,
    port: int,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    try:
        server = build_server(port=port, connection=connection)
    except OSError as error:
        raise CertificationError(f"cannot bind Hub certification server on port {port}") from error
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _post_mcp(
    port: int,
    path: str,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if session_id is not None:
        headers[MCP_SESSION_HEADER] = session_id
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return _session_id(response, session_id), json.loads(response.read().decode("utf-8"))


def _session_id(response: HTTPResponse, fallback: str | None) -> str:
    value = response.headers.get(MCP_SESSION_HEADER)
    if value:
        return value
    if fallback:
        return fallback
    raise CertificationError("Hub response did not include a frontend session id")


__all__ = ["ClineCertificationResult", "run_cline_certification"]
