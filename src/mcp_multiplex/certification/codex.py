"""Codex CLI real-client certification harness."""

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

from mcp_multiplex.adapters import parse_codex_config
from mcp_multiplex.apply import apply_plan, rollback_backup
from mcp_multiplex.approvals import ApprovalStore
from mcp_multiplex.catalog import CatalogStore, match_observed_entry
from mcp_multiplex.daemon import MCP_SESSION_HEADER, build_server
from mcp_multiplex.install import install_codex_control_plane
from mcp_multiplex.observability import EventStore, ObservedEntryStore, classify_observed_entries
from mcp_multiplex.planning import generate_known_direct_rewrite_plan
from mcp_multiplex.schemas import CatalogEntry, RemediationPlan
from mcp_multiplex.storage import connect

HUB_BASE_URL = "http://127.0.0.1:30000"
CODEX_AGENT_ID = "agent_codex_user_default"
CODEX_CONTROL_TOKEN_ENV_VAR = "MCP_MULTIPLEX_CONTROL_TOKEN"
CREATED_AT = "2026-06-21T00:00:00Z"


class CertificationError(RuntimeError):
    """Raised when real-client certification cannot pass."""


@dataclass(frozen=True)
class CertificationStep:
    """One certification step result."""

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
class CodexCertificationResult:
    """TASK-038 Codex certification result."""

    ok: bool
    work_dir: str
    codex_version: str
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
            "kind": "MCPMultiplexCodexCertification",
            "ok": self.ok,
            "work_dir": self.work_dir,
            "codex_version": self.codex_version,
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
            "# TASK-038 Codex CLI Certification",
            "",
            f"Result: {'PASS' if self.ok else 'FAIL'}",
            f"Work dir: `{self.work_dir}`",
            f"Codex version: `{self.codex_version}`",
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
                "```toml",
                self.config_before.rstrip(),
                "```",
                "",
                "## Config After Apply",
                "",
                "```toml",
                self.config_after.rstrip(),
                "```",
                "",
                "## Config After Rollback",
                "",
                "```toml",
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
                "Final human approval is still required before upgrading Codex CLI to certified.",
            ]
        )
        return "\n".join(lines) + "\n"


def run_codex_certification(
    *,
    work_dir: Path | None = None,
    codex_bin: str = "codex",
    port: int = 30000,
) -> CodexCertificationResult:
    """Run TASK-038 Codex certification in an isolated working directory."""
    if port != 30000:
        raise CertificationError("Codex certification must use Hub data-plane port 30000")
    resolved_codex = shutil.which(codex_bin)
    if resolved_codex is None:
        raise CertificationError(f"Codex CLI not found: {codex_bin}")
    root = _work_dir(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    codex_home = root / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    db_path = root / "multiplex.db"
    config_path = codex_home / "config.toml"
    connection = connect(db_path)
    steps: list[CertificationStep] = []

    fake_backend = _fake_backend_path()
    config_path.write_text(_direct_codex_config(fake_backend), encoding="utf-8")
    control_install = install_codex_control_plane(
        connection,
        home=codex_home,
        config_path=config_path,
        backup_dir=root / "install-backups",
        actor="codex_certification",
    )
    if control_install.token is None:
        raise CertificationError("Codex control-plane installer did not issue an auth token")
    config_before = config_path.read_text(encoding="utf-8")
    control_token = control_install.token
    catalog_entry = CatalogEntry.from_dict(_fake_context7_catalog_payload())
    CatalogStore(connection).upsert(catalog_entry)
    steps.append(
        CertificationStep(
            "install_mcp_hub_and_direct_fixture",
            True,
            "installed authenticated mcp_hub with the Codex installer and direct context7 fixture",
            {
                "config_path": str(config_path),
                "install_backup_id": control_install.backup.backup_id
                if control_install.backup is not None
                else None,
                "token_ref": control_token.token_ref,
            },
        )
    )

    parsed_before = parse_codex_config(config_path)
    ObservedEntryStore(connection).upsert_many(parsed_before.observed_entries)
    classifications = classify_observed_entries(parsed_before.observed_entries)
    direct = next(item for item in classifications if item.observed_entry.mount_name == "context7")
    if direct.classification != "active_direct_bypass":
        raise CertificationError("direct context7 fixture did not produce active_direct_bypass")
    steps.append(
        CertificationStep(
            "detect_drift",
            True,
            "Codex direct context7 was detected as an active direct bypass",
            {"classification": direct.classification},
        )
    )

    observed_context7 = direct.observed_entry
    match = match_observed_entry(observed_context7, [catalog_entry])
    plan = generate_known_direct_rewrite_plan(
        observed_context7,
        catalog_entry,
        match,
        created_at=CREATED_AT,
    )
    _insert_plan(connection, plan)
    approval = ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)
    approval = ApprovalStore(connection).approve(
        approval.approval_id,
        actor="codex_certification",
        channel="cli",
        decided_at=CREATED_AT,
        comment="TASK-038 disposable certification approval",
    )
    apply_result = apply_plan(
        connection,
        plan.plan_id,
        actor="codex_certification",
        backup_dir=root / "backups",
    )
    config_after = config_path.read_text(encoding="utf-8")
    if f'url = "{HUB_BASE_URL}/servers/context7/mcp"' not in config_after:
        raise CertificationError("Codex config was not rewritten to the Hub URL")
    steps.append(
        CertificationStep(
            "rewrite_through_hub",
            True,
            "approved plan rewrote context7 through the Hub and created a backup",
            {"plan_id": plan.plan_id, "backup_id": apply_result.backup.backup_id},
        )
    )

    codex_version = _run_codex(
        [resolved_codex, "--version"],
        codex_home=codex_home,
        control_token=control_token.token,
    ).strip()
    codex_list = _run_codex(
        [resolved_codex, "mcp", "list", "--json"],
        codex_home=codex_home,
        control_token=control_token.token,
    )
    codex_servers = json.loads(codex_list)
    _assert_codex_server_url(codex_servers, "mcp_hub", f"{HUB_BASE_URL}/servers/mcp_hub/mcp")
    _assert_codex_server_auth(codex_servers, "mcp_hub")
    _assert_codex_server_url(codex_servers, "context7", f"{HUB_BASE_URL}/servers/context7/mcp")
    steps.append(
        CertificationStep(
            "verify_codex_sees_hub_routed_mcp",
            True,
            "real Codex CLI listed mcp_hub and context7 as Hub-routed MCP entries",
            {"servers": _redacted_codex_servers(codex_servers)},
        )
    )

    server, thread = _start_runtime_server(connection, port=port)
    try:
        hub_session_id, hub_initialize_body = _post_mcp(
            port,
            "/servers/mcp_hub/mcp",
            {"jsonrpc": "2.0", "id": "hub-init-cert", "method": "initialize"},
            authorization_token=control_token.token,
        )
        _, hub_self_check_body = _post_mcp(
            port,
            "/servers/mcp_hub/mcp",
            {
                "jsonrpc": "2.0",
                "id": "hub-self-check-cert",
                "method": "tools/call",
                "params": {"name": "self_check", "arguments": {}},
            },
            session_id=hub_session_id,
            authorization_token=control_token.token,
        )
        session_id, initialize_body = _post_mcp(
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
    self_check = _control_tool_json_result(hub_self_check_body)
    if self_check.get("kind") != "MCPHubSelfCheck" or self_check.get("agent_id") != CODEX_AGENT_ID:
        raise CertificationError("authenticated mcp_hub.self_check did not return Codex scope")
    steps.append(
        CertificationStep(
            "authenticated_mcp_hub_self_check",
            True,
            "mcp_hub initialized and returned agent-scoped self_check with bearer auth",
            {
                "initialize": hub_initialize_body,
                "self_check": {
                    "kind": self_check.get("kind"),
                    "agent_id": self_check.get("agent_id"),
                    "ok": self_check.get("ok"),
                    "destructive_actions": self_check.get("destructive_actions"),
                    "destructive_actions_require_approval": self_check.get(
                        "destructive_actions_require_approval"
                    ),
                },
            },
        )
    )
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
            {
                "initialize": initialize_body,
                "tool_call": tool_body,
                "runtime_event_types": [event["event_type"] for event in runtime_events],
            },
        )
    )

    rollback = rollback_backup(
        connection,
        apply_result.backup.backup_id,
        actor="codex_certification",
    )
    config_after_rollback = config_path.read_text(encoding="utf-8")
    if config_after_rollback != config_before:
        raise CertificationError("rollback did not restore exact Codex config bytes")
    steps.append(
        CertificationStep(
            "rollback",
            True,
            "rollback restored exact pre-image bytes",
            {"restored_hash": rollback.restored_hash},
        )
    )

    return CodexCertificationResult(
        ok=True,
        work_dir=str(root),
        codex_version=codex_version,
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
    return Path(tempfile.mkdtemp(prefix="mcp-multiplex-codex-cert-")).resolve()


def _direct_codex_config(fake_backend: Path) -> str:
    return f'[mcp_servers.context7]\ncommand = "{sys.executable}"\nargs = ["{fake_backend}"]\n'


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


def _run_codex(
    command: list[str],
    *,
    codex_home: Path,
    control_token: str | None = None,
) -> str:
    env = {**os.environ, "CODEX_HOME": str(codex_home)}
    if control_token is not None:
        env[CODEX_CONTROL_TOKEN_ENV_VAR] = control_token
    result = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )
    if result.returncode != 0:
        raise CertificationError(
            f"Codex command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result.stdout


def _assert_codex_server_url(
    servers: list[dict[str, Any]],
    name: str,
    expected_url: str,
) -> None:
    server = next((item for item in servers if item.get("name") == name), None)
    if server is None:
        raise CertificationError(f"Codex did not list MCP server {name}")
    transport = server.get("transport")
    if not isinstance(transport, dict) or transport.get("url") != expected_url:
        raise CertificationError(f"Codex server {name} was not Hub-routed to {expected_url}")


def _assert_codex_server_auth(servers: list[dict[str, Any]], name: str) -> None:
    server = next((item for item in servers if item.get("name") == name), None)
    if server is None:
        raise CertificationError(f"Codex did not list MCP server {name}")
    transport = server.get("transport")
    if not isinstance(transport, dict):
        raise CertificationError(f"Codex server {name} did not expose transport metadata")
    if transport.get("bearer_token_env_var") != CODEX_CONTROL_TOKEN_ENV_VAR:
        raise CertificationError(f"Codex server {name} did not use bearer token env var")


def _redacted_codex_servers(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    redacted = []
    for server in servers:
        transport = dict(server.get("transport") or {})
        transport.pop("bearer_token_env_var", None)
        redacted.append(
            {
                "name": server.get("name"),
                "enabled": server.get("enabled"),
                "transport": transport,
                "auth_status": server.get("auth_status"),
            }
        )
    return redacted


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
    authorization_token: str | None = None,
) -> tuple[str, dict[str, Any]]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if session_id is not None:
        headers[MCP_SESSION_HEADER] = session_id
    if authorization_token is not None:
        headers["Authorization"] = f"Bearer {authorization_token}"
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


def _control_tool_json_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("isError") is not False:
        raise CertificationError("mcp_hub tool call returned an error")
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        raise CertificationError("mcp_hub tool call returned invalid content")
    text = content[0].get("text")
    if not isinstance(text, str):
        raise CertificationError("mcp_hub tool call returned non-text content")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise CertificationError("mcp_hub tool call returned non-object JSON")
    return parsed
