"""Gemini CLI real-client certification harness."""

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

from mcp_multiplex.adapters import parse_gemini_config
from mcp_multiplex.apply import apply_plan, rollback_backup
from mcp_multiplex.approvals import ApprovalStore
from mcp_multiplex.catalog import CatalogStore, match_observed_entry
from mcp_multiplex.certification.codex import CertificationError
from mcp_multiplex.daemon import MCP_SESSION_HEADER, build_server
from mcp_multiplex.install import install_gemini_control_plane
from mcp_multiplex.observability import EventStore, ObservedEntryStore, classify_observed_entries
from mcp_multiplex.planning import generate_known_direct_rewrite_plan
from mcp_multiplex.schemas import CatalogEntry, RemediationPlan
from mcp_multiplex.storage import connect

HUB_BASE_URL = "http://127.0.0.1:30000"
GEMINI_AGENT_ID = "agent_gemini_user_default"
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
class GeminiCertificationResult:
    ok: bool
    work_dir: str
    gemini_version: str
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
            "kind": "MCPMultiplexGeminiCertification",
            "ok": self.ok,
            "work_dir": self.work_dir,
            "gemini_version": self.gemini_version,
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
            "# TASK-040 Gemini CLI Certification",
            "",
            f"Result: {'PASS' if self.ok else 'FAIL'}",
            f"Work dir: `{self.work_dir}`",
            f"Gemini CLI version: `{self.gemini_version}`",
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
                "Final human approval is still required before upgrading Gemini CLI to certified.",
            ]
        )
        return "\n".join(lines) + "\n"


def run_gemini_certification(
    *,
    work_dir: Path | None = None,
    gemini_bin: str = "gemini",
    port: int = 30000,
) -> GeminiCertificationResult:
    if port != 30000:
        raise CertificationError("Gemini certification must use Hub data-plane port 30000")
    resolved_gemini = shutil.which(gemini_bin)
    if resolved_gemini is None:
        raise CertificationError(f"Gemini CLI not found: {gemini_bin}")
    root = _work_dir(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    home = root / "home"
    settings_dir = home / ".gemini"
    settings_dir.mkdir(parents=True, exist_ok=True)
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    config_path = settings_dir / "settings.json"
    connection = connect(root / "multiplex.db")
    steps: list[CertificationStep] = []

    config_path.write_text(_direct_gemini_config(), encoding="utf-8")
    control_install = install_gemini_control_plane(
        connection,
        home=home,
        config_path=config_path,
        backup_dir=root / "install-backups",
        actor="gemini_certification",
    )
    if control_install.token is None:
        raise CertificationError("Gemini control-plane installer did not issue an auth token")
    control_token = control_install.token
    config_before = config_path.read_text(encoding="utf-8")
    catalog_entry = CatalogEntry.from_dict(_fake_context7_catalog_payload())
    CatalogStore(connection).upsert(catalog_entry)
    steps.append(
        CertificationStep(
            "install_mcp_hub_and_direct_fixture",
            True,
            "installed authenticated mcp_hub with the Gemini installer and direct context7 fixture",
            {
                "config_path": str(config_path),
                "install_backup_id": control_install.backup.backup_id
                if control_install.backup is not None
                else None,
                "token_ref": control_token.token_ref,
            },
        )
    )

    parsed_before = parse_gemini_config(config_path, agent_id=GEMINI_AGENT_ID)
    ObservedEntryStore(connection).upsert_many(parsed_before.observed_entries)
    classifications = classify_observed_entries(parsed_before.observed_entries)
    direct = next(item for item in classifications if item.observed_entry.mount_name == "context7")
    if direct.classification != "active_direct_bypass":
        raise CertificationError("direct context7 fixture did not produce active_direct_bypass")
    steps.append(
        CertificationStep(
            "detect_drift",
            True,
            "Gemini direct context7 was detected as an active direct bypass",
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
        actor="gemini_certification",
        channel="cli",
        decided_at=CREATED_AT,
        comment="TASK-040 disposable certification approval",
    )
    apply_result = apply_plan(
        connection,
        plan.plan_id,
        actor="gemini_certification",
        backup_dir=root / "backups",
    )
    config_after = config_path.read_text(encoding="utf-8")
    if f'"httpUrl": "{HUB_BASE_URL}/servers/context7/mcp"' not in config_after:
        raise CertificationError("Gemini config was not rewritten to httpUrl Hub URL")
    steps.append(
        CertificationStep(
            "rewrite_through_hub",
            True,
            "approved plan rewrote context7 through the Hub using Gemini httpUrl",
            {"plan_id": plan.plan_id, "backup_id": apply_result.backup.backup_id},
        )
    )

    server, thread = _start_runtime_server(connection, port=port)
    try:
        gemini_version = _run_gemini(
            [resolved_gemini, "--version"],
            home=home,
            cwd=project,
            control_token=control_token.token,
        ).strip()
        _assert_no_gemini_config_error(gemini_version)
        gemini_list = _run_gemini(
            [resolved_gemini, "mcp", "list"],
            home=home,
            cwd=project,
            control_token=control_token.token,
        )
        _assert_no_gemini_config_error(gemini_list)
        if f"context7: {HUB_BASE_URL}/servers/context7/mcp" not in gemini_list:
            raise CertificationError("Gemini did not list context7 as Hub-routed")
        if f"mcp_hub: {HUB_BASE_URL}/servers/mcp_hub/mcp" not in gemini_list:
            raise CertificationError("Gemini did not list mcp_hub as Hub-routed")
        steps.append(
            CertificationStep(
                "verify_gemini_sees_hub_routed_mcp",
                True,
                "real Gemini CLI listed mcp_hub and context7 as Hub-routed MCP entries",
                {"output": gemini_list},
            )
        )
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
        actor="gemini_certification",
    )
    config_after_rollback = config_path.read_text(encoding="utf-8")
    if config_after_rollback != config_before:
        raise CertificationError("rollback did not restore exact Gemini settings bytes")
    steps.append(
        CertificationStep(
            "rollback",
            True,
            "rollback restored exact pre-image bytes",
            {"restored_hash": rollback.restored_hash},
        )
    )
    return GeminiCertificationResult(
        ok=True,
        work_dir=str(root),
        gemini_version=gemini_version,
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
    return Path(tempfile.mkdtemp(prefix="mcp-multiplex-gemini-cert-")).resolve()


def _direct_gemini_config() -> str:
    payload = {
        "security": {"folderTrust": {"enabled": False}},
        "mcpServers": {
            "context7": {
                "command": sys.executable,
                "args": [str(_fake_backend_path())],
                "trust": True,
            },
        },
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


def _run_gemini(
    command: list[str],
    *,
    home: Path,
    cwd: Path,
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
        cwd=cwd,
        env=env,
    )
    if result.returncode != 0:
        raise CertificationError(
            f"Gemini command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result.stdout + result.stderr


def _assert_no_gemini_config_error(output: str) -> None:
    if "Invalid configuration" in output:
        raise CertificationError(f"Gemini reported invalid configuration:\n{output}")


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


__all__ = ["GeminiCertificationResult", "run_gemini_certification"]
