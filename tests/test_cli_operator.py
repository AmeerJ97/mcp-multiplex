from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mcp_multiplex.adapters import AgentConfigPath, AgentRegistry, parse_codex_config
from mcp_multiplex.approvals import ApprovalStore
from mcp_multiplex.catalog import CatalogCandidateStore, CatalogStore, match_observed_entry
from mcp_multiplex.cli import main as cli_main
from mcp_multiplex.credentials import CredentialRefStore
from mcp_multiplex.observability import EventStore, ObservedEntryStore
from mcp_multiplex.planning import generate_known_direct_rewrite_plan
from mcp_multiplex.runtime import RuntimeBackendStore
from mcp_multiplex.schemas import CatalogCandidate, CatalogEntry, ObservedEntry, RemediationPlan
from mcp_multiplex.storage import connect
from tests.test_catalog_matching import catalog_entry

CREATED_AT = "2026-06-20T00:00:00Z"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "multiplex.db"


def test_cli_status_json_and_compact_report_local_state(
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = connect(db_path)
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    CatalogStore(connection).upsert(CatalogEntry.from_dict(catalog_entry().to_dict()))
    observed = _write_direct_config(db_path.parent).read_text(encoding="utf-8")
    config_path = db_path.parent / "config.toml"
    config_path.write_text(observed, encoding="utf-8")
    ObservedEntryStore(connection).upsert_many(
        [parse_codex_config(config_path).observed_entries[0]]
    )
    CredentialRefStore(connection).create(
        catalog_id="srv_context7",
        name="SERVICE_TOKEN",
        source_kind="env",
        source_ref="secretref:env/SERVICE_TOKEN",
    )

    exit_code = cli_main(["status", "--json", "--db-path", str(db_path)])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexHealth"
    assert payload["summary"]["blockers"] == 1
    assert payload["blockers"][0]["code"] == "active_direct_bypass"

    exit_code = cli_main(["status", "--compact", "--db-path", str(db_path)])

    assert exit_code == 1
    assert "blocked: 1 blockers" in capsys.readouterr().out


def test_cli_plan_list_show_and_catalog_candidates(
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = connect(db_path)
    plan = _insert_minimal_plan(connection)
    CatalogCandidateStore(connection).upsert(
        CatalogCandidate.from_dict(
            {
                "schema_version": 1,
                "candidate_id": "cand_cli",
                "source": "observed_agent_config",
                "observed_entry_id": "obs_cli",
                "proposed_name": "cli-test",
                "classification": "unknown_stdio",
                "review_state": "pending",
                "risk_tier": "unknown",
                "confidence": "low",
                "backend_shape": {"type": "stdio", "command": "uvx", "args": ["cli-test"]},
                "approval_required": True,
                "reasons": ["not_in_catalog"],
            }
        )
    )

    assert cli_main(["plan", "list", "--db-path", str(db_path)]) == 0
    list_payload = json.loads(capsys.readouterr().out)
    assert list_payload["plans"][0]["plan_id"] == plan.plan_id

    assert cli_main(["plan", "show", plan.plan_id, "--db-path", str(db_path)]) == 0
    show_payload = json.loads(capsys.readouterr().out)
    assert show_payload["plan"]["diff"]["format"] == "unified"

    assert cli_main(["catalog", "candidates", "--db-path", str(db_path)]) == 0
    candidates_payload = json.loads(capsys.readouterr().out)
    assert candidates_payload["candidates"][0]["candidate_id"] == "cand_cli"


def test_cli_catalog_review_updates_state_and_audits(
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = connect(db_path)
    payload = catalog_entry().to_dict()
    payload["review_state"] = "pending"
    payload["lifecycle_state"] = "disabled"
    CatalogStore(connection).upsert(CatalogEntry.from_dict(payload))

    assert (
        cli_main(
            [
                "catalog",
                "review",
                "srv_context7",
                "--review-state",
                "approved",
                "--lifecycle-state",
                "enabled",
                "--actor",
                "test_operator",
                "--comment",
                "reviewed legacy import",
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )

    review_payload = json.loads(capsys.readouterr().out)
    assert review_payload["kind"] == "MCPMultiplexCatalogReview"
    assert review_payload["entry"]["review_state"] == "approved"
    assert review_payload["entry"]["lifecycle_state"] == "enabled"
    assert review_payload["routability"]["routable"] is True
    events = EventStore(connection).query(event_type="catalog.reviewed")
    assert len(events) == 1
    assert events[0].event.actor == "test_operator"
    assert events[0].payload["comment"] == "reviewed legacy import"


def test_cli_runtime_ps_why_slow_and_release_gate(
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = connect(db_path)
    CatalogStore(connection).upsert(CatalogEntry.from_dict(catalog_entry().to_dict()))
    RuntimeBackendStore(connection).create_starting(
        catalog_id="srv_context7",
        runtime_pool_key="global:catalog:srv_context7",
        pid=123,
    )

    assert cli_main(["runtime", "ps", "--db-path", str(db_path)]) == 0
    ps_payload = json.loads(capsys.readouterr().out)
    assert ps_payload["backends"][0]["catalog_id"] == "srv_context7"

    assert cli_main(["runtime", "why-slow", "--server", "context7", "--db-path", str(db_path)]) == 0
    why_payload = json.loads(capsys.readouterr().out)
    assert why_payload["diagnostics"][0]["sharing_explanation"].startswith("shared globally")

    assert cli_main(["doctor", "release-gate", "--db-path", str(db_path)]) == 0
    gate_payload = json.loads(capsys.readouterr().out)
    assert gate_payload["ok"] is True
    assert {check["name"] for check in gate_payload["checks"]} == {
        "status_ok",
        "no_active_direct_bypass",
        "real_client_certifications",
        "mcp_hub_auth",
        "audit_secret_redaction",
        "runtime_share_policy",
        "audit_hash_chain",
    }


def test_cli_agents_install_control_plane_for_codex_dry_run_and_apply(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8")

    assert (
        cli_main(
            [
                "agents",
                "install-control-plane",
                "--agent",
                "codex",
                "--config-path",
                str(config_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    dry_run_payload = json.loads(capsys.readouterr().out)
    assert dry_run_payload["mode"] == "dry_run"
    assert dry_run_payload["result"]["would_change"] is True
    assert "mcp_hub" not in config_path.read_text(encoding="utf-8")

    assert (
        cli_main(
            [
                "agents",
                "install-control-plane",
                "--agent",
                "codex",
                "--apply",
                "--config-path",
                str(config_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    apply_payload = json.loads(captured.out)
    assert apply_payload["mode"] == "apply"
    assert apply_payload["result"]["token"]["token"] == "[REDACTED]"
    assert apply_payload["result"]["backup"]["backup_id"].startswith("bak_")
    assert "Control token was issued and redacted" in captured.err
    config_text = config_path.read_text(encoding="utf-8")
    assert "[mcp_servers.mcp_hub]" in config_text
    assert 'bearer_token_env_var = "MCP_MULTIPLEX_CONTROL_TOKEN"' in config_text


def test_cli_agents_install_control_plane_for_cline_dry_run_and_apply(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "cline_mcp_settings.json"
    helper_path = tmp_path / ".mcp-multiplex" / "cline-mcp-multiplex-remote.sh"
    config_path.write_text('{"mcpServers":{"context7":{"command":"npx"}}}\n', encoding="utf-8")

    assert (
        cli_main(
            [
                "agents",
                "install-control-plane",
                "--agent",
                "cline",
                "--config-path",
                str(config_path),
                "--helper-path",
                str(helper_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    dry_run_payload = json.loads(capsys.readouterr().out)
    assert dry_run_payload["mode"] == "dry_run"
    assert dry_run_payload["result"]["would_change"] is True
    assert "mcp_hub" not in config_path.read_text(encoding="utf-8")
    assert not helper_path.exists()

    assert (
        cli_main(
            [
                "agents",
                "install-control-plane",
                "--agent",
                "cline",
                "--apply",
                "--config-path",
                str(config_path),
                "--helper-path",
                str(helper_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    apply_payload = json.loads(captured.out)
    assert apply_payload["mode"] == "apply"
    assert apply_payload["result"]["token"]["token"] == "[REDACTED]"
    assert apply_payload["result"]["helper_backup"]["backup_id"].startswith("bak_")
    assert "Control token was issued and redacted" in captured.err
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["mcp_hub"]["command"] == str(helper_path.resolve())
    assert helper_path.exists()


def test_cli_agents_install_control_plane_for_claude_code_dry_run_and_apply(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / ".claude.json"
    helper_path = tmp_path / ".mcp-multiplex" / "claude-helper.sh"
    config_path.write_text('{"mcpServers":{"context7":{"command":"npx"}}}\n', encoding="utf-8")

    assert (
        cli_main(
            [
                "agents",
                "install-control-plane",
                "--agent",
                "claude_code",
                "--config-path",
                str(config_path),
                "--helper-path",
                str(helper_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    dry_run_payload = json.loads(capsys.readouterr().out)
    assert dry_run_payload["mode"] == "dry_run"
    assert dry_run_payload["result"]["would_change"] is True
    assert "mcp_hub" not in config_path.read_text(encoding="utf-8")
    assert not helper_path.exists()

    assert (
        cli_main(
            [
                "agents",
                "install-control-plane",
                "--agent",
                "claude_code",
                "--apply",
                "--config-path",
                str(config_path),
                "--helper-path",
                str(helper_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    apply_payload = json.loads(captured.out)
    assert apply_payload["mode"] == "apply"
    assert apply_payload["result"]["token"]["token"] == "[REDACTED]"
    assert apply_payload["result"]["helper_backup"]["backup_id"].startswith("bak_")
    assert "Control token was issued and redacted" in captured.err
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["mcp_hub"]["headersHelper"] == str(helper_path.resolve())
    assert helper_path.exists()


def test_cli_agents_install_control_plane_for_opencode_dry_run_and_apply(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "opencode.jsonc"
    config_path.write_text('{"mcp":{"context7":{"type":"remote","url":"http://x/mcp"}}}\n')

    assert (
        cli_main(
            [
                "agents",
                "install-control-plane",
                "--agent",
                "opencode",
                "--config-path",
                str(config_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    dry_run_payload = json.loads(capsys.readouterr().out)
    assert dry_run_payload["mode"] == "dry_run"
    assert dry_run_payload["result"]["would_change"] is True
    assert "mcp_hub" not in config_path.read_text(encoding="utf-8")

    assert (
        cli_main(
            [
                "agents",
                "install-control-plane",
                "--agent",
                "opencode",
                "--apply",
                "--config-path",
                str(config_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    apply_payload = json.loads(captured.out)
    assert apply_payload["mode"] == "apply"
    assert apply_payload["result"]["token"]["token"] == "[REDACTED]"
    assert "Control token was issued and redacted" in captured.err
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcp"]["mcp_hub"]["headers"] == {
        "Authorization": "Bearer {env:MCP_MULTIPLEX_CONTROL_TOKEN}"
    }
    assert config["mcp"]["mcp_hub"]["oauth"] is False


def test_cli_agents_install_control_plane_for_gemini_dry_run_and_apply(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "settings.json"
    config_path.write_text('{"mcpServers":{"context7":{"httpUrl":"http://x/mcp"}}}\n')

    assert (
        cli_main(
            [
                "agents",
                "install-control-plane",
                "--agent",
                "gemini",
                "--config-path",
                str(config_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    dry_run_payload = json.loads(capsys.readouterr().out)
    assert dry_run_payload["mode"] == "dry_run"
    assert dry_run_payload["result"]["would_change"] is True
    assert "mcp_hub" not in config_path.read_text(encoding="utf-8")

    assert (
        cli_main(
            [
                "agents",
                "install-control-plane",
                "--agent",
                "gemini",
                "--apply",
                "--config-path",
                str(config_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    apply_payload = json.loads(captured.out)
    assert apply_payload["mode"] == "apply"
    assert apply_payload["result"]["token"]["token"] == "[REDACTED]"
    assert "Control token was issued and redacted" in captured.err
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["mcp_hub"]["headers"] == {
        "Authorization": "Bearer $MCP_MULTIPLEX_CONTROL_TOKEN"
    }
    assert config["mcpServers"]["mcp_hub"]["httpUrl"] == (
        "http://127.0.0.1:30000/servers/mcp_hub/mcp"
    )


def test_cli_agents_auth_capabilities_reports_first_wave_matrix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["agents", "auth-capabilities"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexControlPlaneAuthCapabilities"
    capabilities = {item["agent_kind"]: item for item in payload["capabilities"]}
    assert set(capabilities) == {"codex", "claude_code", "gemini", "cline", "opencode"}
    assert capabilities["codex"]["automatic_install_supported"] is True
    assert capabilities["codex"]["raw_token_storage_required"] is False
    assert capabilities["claude_code"]["automatic_install_supported"] is True
    assert capabilities["claude_code"]["raw_token_storage_required"] is False
    assert capabilities["opencode"]["automatic_install_supported"] is True
    assert capabilities["opencode"]["raw_token_storage_required"] is False
    assert capabilities["gemini"]["automatic_install_supported"] is True
    assert capabilities["gemini"]["raw_token_storage_required"] is False
    assert capabilities["cline"]["automatic_install_supported"] is True
    assert capabilities["cline"]["raw_token_storage_required"] is False


def test_cli_agents_self_check_reports_ready_agent(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = connect(db_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
        certification_level="certified",
        config_paths=[AgentConfigPath(path=str(config_path), format="toml", precedence=10)],
    )
    ObservedEntryStore(connection).upsert_many([_observed_mcp_hub(config_path)])

    assert (
        cli_main(
            [
                "agents",
                "self-check",
                "--agent",
                "codex",
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexAgentSelfCheck"
    assert payload["ok"] is True
    assert payload["not_ready_count"] == 0
    assert payload["agents"][0]["agent_kind"] == "codex"
    assert payload["agents"][0]["self_check"] == "ready"
    assert payload["agents"][0]["mcp_hub_entries"][0]["authenticated"] is True


def test_cli_agents_self_check_exits_nonzero_when_mcp_hub_missing(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = connect(db_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
        certification_level="certified",
        config_paths=[AgentConfigPath(path=str(config_path), format="toml", precedence=10)],
    )

    assert (
        cli_main(
            [
                "agents",
                "self-check",
                "--agent",
                "codex",
                "--db-path",
                str(db_path),
            ]
        )
        == 1
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexAgentSelfCheck"
    assert payload["ok"] is False
    assert payload["not_ready_count"] == 1
    assert payload["agents"][0]["self_check"] == "needs_install_or_audit"
    assert payload["agents"][0]["mcp_hub_entries"] == []


def test_cli_apply_and_rollback_use_approved_plan(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = connect(db_path)
    config_path = _write_direct_config(tmp_path)
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    plan = _store_known_direct_plan(connection, config_path)
    pending = ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)
    ApprovalStore(connection).approve(pending.approval_id, actor="local_operator", channel="cli")

    assert cli_main(["apply", plan.plan_id, "--db-path", str(db_path)]) == 0
    apply_payload = json.loads(capsys.readouterr().out)
    assert apply_payload["ok"] is True
    backup_id = apply_payload["result"]["backup"]["backup_id"]
    assert 'url = "http://127.0.0.1:30000/servers/context7/mcp"' in config_path.read_text(
        encoding="utf-8"
    )

    assert cli_main(["rollback", backup_id, "--db-path", str(db_path)]) == 0
    rollback_payload = json.loads(capsys.readouterr().out)
    assert rollback_payload["ok"] is True
    assert 'command = "npx"' in config_path.read_text(encoding="utf-8")


def test_cli_audit_run_uses_registered_config_paths(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_direct_config(tmp_path)
    connection = connect(db_path)
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
        config_paths=[AgentConfigPath(path=str(config_path), format="toml")],
    )

    exit_code = cli_main(["audit", "run", "--db-path", str(db_path), "--run-id", "cli_test"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexAuditRun"
    assert payload["target_count"] == 1
    assert payload["result"]["health"]["blockers"][0]["code"] == "active_direct_bypass"


def test_cli_audit_plan_stores_dry_run_remediation_plans(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_direct_config(tmp_path)
    connection = connect(db_path)
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
        config_paths=[AgentConfigPath(path=str(config_path), format="toml")],
    )
    CatalogStore(connection).upsert(CatalogEntry.from_dict(catalog_entry().to_dict()))

    assert (
        cli_main(
            [
                "audit",
                "plan",
                "--db-path",
                str(db_path),
                "--run-id",
                "cli_plan_test",
                "--skip-missing-control-plane",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexAuditPlan"
    assert payload["target_count"] == 1
    assert payload["plan_count"] == 1
    assert payload["inserted_count"] == 1
    plan_id = payload["inserted_plan_ids"][0]
    plan = _plan_row_for_test(connection, plan_id)
    assert plan["plan_type"] == "rewrite_known_direct"
    assert plan["status"] == "pending_approval"
    assert plan["policy"]["approval_required"] is True


def _write_direct_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "@upstash/context7-mcp"]\n',
        encoding="utf-8",
    )
    return config_path


def _observed_mcp_hub(config_path: Path) -> ObservedEntry:
    return ObservedEntry.from_dict(
        {
            "schema_version": 1,
            "observed_entry_id": "obs_cli_mcp_hub",
            "agent_id": "agent_codex_user_default",
            "agent_kind": "codex",
            "config_path": str(config_path),
            "container_path": ["mcp_servers", "mcp_hub"],
            "mount_name": "mcp_hub",
            "enabled": True,
            "transport": "streamable_http",
            "command": None,
            "args": [],
            "url": "http://127.0.0.1:30000/servers/mcp_hub/mcp",
            "headers_present": ["Authorization"],
            "env_names": [],
            "cwd": None,
            "tool_filters": {"enabled_tools": None, "disabled_tools": []},
            "approval_policy": None,
            "entry_hash": "sha256:" + "3" * 64,
            "raw_shape": "codex-toml",
            "parser_confidence": "complete",
        }
    )


def _plan_row_for_test(connection: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT plan_type, status, policy_json FROM remediation_plans WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    assert row is not None
    return {
        "plan_type": str(row["plan_type"]),
        "status": str(row["status"]),
        "policy": json.loads(str(row["policy_json"])),
    }


def _insert_minimal_plan(connection: sqlite3.Connection) -> RemediationPlan:
    row = connection.execute("PRAGMA database_list").fetchone()
    db_parent = Path(str(row["file"])).parent
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    return _store_known_direct_plan(connection, _write_direct_config(db_parent))


def _store_known_direct_plan(connection: sqlite3.Connection, config_path: Path) -> RemediationPlan:
    observed = parse_codex_config(config_path).observed_entries[0]
    ObservedEntryStore(connection).upsert_many([observed])
    catalog = catalog_entry()
    CatalogStore(connection).upsert(CatalogEntry.from_dict(catalog.to_dict()))
    match = match_observed_entry(observed, [catalog])
    plan = generate_known_direct_rewrite_plan(
        observed,
        CatalogEntry.from_dict(catalog.to_dict()),
        match,
        created_at=CREATED_AT,
    )
    _insert_plan(connection, plan)
    return plan


def _insert_plan(connection: sqlite3.Connection, plan: RemediationPlan) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO agents (agent_id, agent_kind, display_name)
            VALUES (?, 'codex', 'Codex CLI')
            ON CONFLICT(agent_id) DO NOTHING
            """,
            (plan.agent_id,),
        )
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
                json.dumps(plan.policy),
                plan.diff.format,
                plan.diff.text,
                plan.expected_preimage_hash,
                plan.rollback_strategy,
                json.dumps(plan.risk),
                plan.created_at,
            ),
        )
