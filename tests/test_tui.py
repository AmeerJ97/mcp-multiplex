from __future__ import annotations

import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path

import pytest

from mcp_multiplex.adapters import AgentRegistry, parse_codex_config
from mcp_multiplex.apply import ConfigBackupStore
from mcp_multiplex.approvals import ApprovalStore
from mcp_multiplex.catalog import CatalogCandidateStore, CatalogStore, match_observed_entry
from mcp_multiplex.cli import main as cli_main
from mcp_multiplex.credentials import CredentialRefStore
from mcp_multiplex.observability import EventStore, ObservedEntryStore
from mcp_multiplex.planning import generate_known_direct_rewrite_plan
from mcp_multiplex.runtime import RuntimeBackendStore
from mcp_multiplex.schemas import CatalogCandidate, CatalogEntry, ObservedEntry, RemediationPlan
from mcp_multiplex.storage import connect
from mcp_multiplex.tui import handle_repl_command, render_tui, run_tui_repl, tui_snapshot
from tests.test_catalog_matching import catalog_entry

CREATED_AT = "2026-06-20T00:00:00Z"


@pytest.fixture
def seeded_tui_db(tmp_path: Path) -> tuple[Path, sqlite3.Connection, str]:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    config_path = _write_direct_config(tmp_path)
    observed = parse_codex_config(config_path).observed_entries[0]
    warning = _disabled_observed_entry(tmp_path)
    ObservedEntryStore(connection).upsert_many(
        [observed, warning, _mcp_hub_observed_entry(tmp_path)]
    )

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
    approval = ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)
    CatalogCandidateStore(connection).upsert(_catalog_candidate(warning.observed_entry_id))
    RuntimeBackendStore(connection).create_starting(
        catalog_id="srv_context7",
        runtime_pool_key="global:catalog:srv_context7",
        pid=123,
    )
    CredentialRefStore(connection).create(
        catalog_id="srv_context7",
        name="SERVICE_TOKEN",
        source_kind="env",
        source_ref="secretref:env/SERVICE_TOKEN",
    )
    backup = ConfigBackupStore(connection).create(
        plan_id=plan.plan_id,
        target_path=config_path,
        content=config_path.read_bytes(),
        created_at=CREATED_AT,
    )
    EventStore(connection).append(
        event_id="evt_tui_change",
        event_type="remediation.applied",
        actor="local_operator",
        result="success",
        plan_id=plan.plan_id,
        target_path=str(config_path),
        backup_id=backup.backup_id,
        timestamp=CREATED_AT,
    )
    EventStore(connection).append(
        event_id="evt_tui_cutover",
        event_type="cutover.applied",
        actor="local_operator",
        result="success",
        payload={
            "source": "mcp-hub",
            "legacy_mcp_hub_deprecated": True,
            "unmanaged_process_action": "none",
        },
        timestamp=CREATED_AT,
    )
    return db_path, connection, approval.approval_id


def test_tui_snapshot_groups_operator_views(
    seeded_tui_db: tuple[Path, sqlite3.Connection, str],
) -> None:
    _, connection, approval_id = seeded_tui_db

    snapshot = tui_snapshot(
        connection,
        legacy_root=Path("/tmp/nonexistent-mcp-hub"),
        home=Path("/tmp/nonexistent-home"),
        include_processes=False,
    )

    assert snapshot["kind"] == "MCPMultiplexTUI"
    assert snapshot["dashboard"]["ok"] is False
    assert snapshot["dashboard"]["summary"]["blockers"] == 2
    assert snapshot["dashboard"]["summary"]["warnings"] == 1
    assert snapshot["problems"]["blockers"][0]["code"] == "active_direct_bypass"
    assert snapshot["problems"]["warnings"][0]["code"] == "disabled_direct_entry"
    assert snapshot["agents"][0]["self_check"] == "ready"
    assert snapshot["agents"][0]["mcp_hub_entries"][0]["authenticated"] is True
    assert snapshot["cutover"]["ok"] is True
    assert snapshot["cutover"]["legacy_mcp_hub_deprecated"] is True
    assert snapshot["cutover"]["cleanup_step_count"] == 0
    assert snapshot["cutover"]["mutation_action"] == "none"
    assert snapshot["approvals"][0]["approval_id"] == approval_id
    assert snapshot["approvals"][0]["plan"]["diff"]["format"] == "unified"
    assert snapshot["approvals"][0]["plan"]["rollback_strategy"] == "restore_backup_before_apply"
    assert snapshot["candidates"][0]["candidate_id"] == "cand_tui"
    assert snapshot["runtime"]["why_slow"][0]["sharing_explanation"].startswith("shared globally")
    assert snapshot["credentials"]["blockers"][0]["name"] == "SERVICE_TOKEN"
    assert snapshot["rollback"]["backups"][0]["plan_id"] == snapshot["approvals"][0]["plan_id"]
    assert snapshot["what_changed"][0]["event_type"] == "remediation.applied"
    assert "secretref" not in json.dumps(snapshot, sort_keys=True)


def test_tui_render_is_human_readable_without_raw_json(
    seeded_tui_db: tuple[Path, sqlite3.Connection, str],
) -> None:
    _, connection, approval_id = seeded_tui_db

    screen = render_tui(
        connection,
        legacy_root=Path("/tmp/nonexistent-mcp-hub"),
        home=Path("/tmp/nonexistent-home"),
        include_processes=False,
    )

    assert screen.snapshot["approvals"][0]["approval_id"] == approval_id
    assert "[Dashboard]" in screen.text
    assert "[Problems]" in screen.text
    assert "[Agents]" in screen.text
    assert "self_check=ready" in screen.text
    assert "[Cutover]" in screen.text
    assert "legacy_mcp_hub_deprecated=yes" in screen.text
    assert "cleanup_steps=0" in screen.text
    assert "Blockers:" in screen.text
    assert "Warnings:" in screen.text
    assert "Notices:" in screen.text
    assert "[Approvals]" in screen.text
    assert "diff:" in screen.text
    assert "rollback: restore_backup_before_apply" in screen.text
    assert "[Candidates]" in screen.text
    assert "[Runtime]" in screen.text
    assert "Why slow:" in screen.text
    assert "[Credentials]" in screen.text
    assert "[Rollback]" in screen.text
    assert "[What Changed]" in screen.text
    assert not screen.text.lstrip().startswith("{")
    assert "secretref" not in screen.text


def test_cli_tui_renders_and_approves_with_tui_channel(
    seeded_tui_db: tuple[Path, sqlite3.Connection, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path, connection, approval_id = seeded_tui_db

    assert cli_main(["tui", "--db-path", str(db_path)]) == 1
    output = capsys.readouterr().out
    assert "MCP Multiplex TUI" in output
    assert approval_id in output

    assert cli_main(["tui", "--approve", approval_id, "--db-path", str(db_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexTUIApprovalDecision"
    assert payload["ok"] is True
    assert payload["approval"]["channel"] == "tui"
    assert payload["plan_status"] == "approved"
    assert ApprovalStore(connection).show(approval_id).state == "approved"


def test_tui_repl_handles_section_commands_and_approval(
    seeded_tui_db: tuple[Path, sqlite3.Connection, str],
) -> None:
    _, connection, approval_id = seeded_tui_db

    dashboard = handle_repl_command(connection, "/dashboard")
    self_check = handle_repl_command(connection, "self-check")
    cutover = handle_repl_command(
        connection,
        "cutover",
        legacy_root=Path("/tmp/nonexistent-mcp-hub"),
        home=Path("/tmp/nonexistent-home"),
        include_processes=False,
    )
    commands = handle_repl_command(connection, "/commands")
    approvals = handle_repl_command(connection, "approvals")
    approved = handle_repl_command(connection, f"approve {approval_id}", actor="repl_test")
    approvals_after = handle_repl_command(connection, "approvals")
    rollback_after = handle_repl_command(connection, "rollback")
    quit_result = handle_repl_command(connection, "quit")

    assert "[Dashboard]" in dashboard["text"]
    assert "[Agents]" in self_check["text"]
    assert "self_check=ready" in self_check["text"]
    assert "[Cutover]" in cutover["text"]
    assert "footprint=clean" in cutover["text"]
    assert "/self-check" in commands["text"]
    assert "/cutover" in commands["text"]
    assert "aliases=agents, agent" in commands["text"]
    assert approval_id in approvals["text"]
    assert f"approved {approval_id}" in approved["text"]
    assert ApprovalStore(connection).show(approval_id).state == "approved"
    assert "- none" in approvals_after["text"]
    assert approval_id not in approvals_after["text"]
    assert ApprovalStore(connection).show(approval_id).plan_id in rollback_after["text"]
    assert quit_result["exit"] is True
    assert quit_result["code"] == 0


def test_tui_repl_loop_is_scriptable(
    seeded_tui_db: tuple[Path, sqlite3.Connection, str],
) -> None:
    _, connection, _ = seeded_tui_db
    stdin = StringIO("dashboard\nself-check\nproblems\nquit\n")
    stdout = StringIO()

    assert run_tui_repl(connection, stdin=stdin, stdout=stdout) == 0

    output = stdout.getvalue()
    assert "MCP Multiplex" in output
    assert "local MCP control plane" in output
    assert "mxp> " in output
    assert "[Dashboard]" in output
    assert "[Agents]" in output
    assert "[Problems]" in output
    assert "bye" in output


def test_cli_tui_repl_uses_stdin(
    seeded_tui_db: tuple[Path, sqlite3.Connection, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _, _ = seeded_tui_db
    monkeypatch.setattr(sys, "stdin", StringIO("dashboard\nquit\n"))

    assert cli_main(["tui", "--repl", "--db-path", str(db_path)]) == 0

    output = capsys.readouterr().out
    assert "MCP Multiplex" in output
    assert "[Dashboard]" in output
    assert "bye" in output


def _write_direct_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "@upstash/context7-mcp"]\n',
        encoding="utf-8",
    )
    return config_path


def _disabled_observed_entry(tmp_path: Path) -> ObservedEntry:
    return ObservedEntry.from_dict(
        {
            "schema_version": 1,
            "observed_entry_id": "obs_tui_disabled",
            "agent_id": "agent_codex_user_default",
            "agent_kind": "codex",
            "config_path": str(tmp_path / "disabled.toml"),
            "container_path": ["mcp_servers", "disabled-tool"],
            "mount_name": "disabled-tool",
            "enabled": False,
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "disabled-tool"],
            "url": None,
            "headers_present": [],
            "env_names": [],
            "cwd": None,
            "tool_filters": {"enabled_tools": None, "disabled_tools": []},
            "approval_policy": None,
            "entry_hash": "sha256:" + "1" * 64,
            "raw_shape": "disabled direct entry",
            "parser_confidence": "complete",
        }
    )


def _mcp_hub_observed_entry(tmp_path: Path) -> ObservedEntry:
    return ObservedEntry.from_dict(
        {
            "schema_version": 1,
            "observed_entry_id": "obs_tui_mcp_hub",
            "agent_id": "agent_codex_user_default",
            "agent_kind": "codex",
            "config_path": str(tmp_path / "config.toml"),
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
            "entry_hash": "sha256:" + "2" * 64,
            "raw_shape": "codex-toml",
            "parser_confidence": "complete",
        }
    )


def _catalog_candidate(observed_entry_id: str) -> CatalogCandidate:
    return CatalogCandidate.from_dict(
        {
            "schema_version": 1,
            "candidate_id": "cand_tui",
            "source": "observed_agent_config",
            "observed_entry_id": observed_entry_id,
            "proposed_name": "disabled-tool",
            "classification": "unknown_stdio",
            "review_state": "pending",
            "risk_tier": "unknown",
            "confidence": "low",
            "backend_shape": {"type": "stdio", "command": "npx", "args": ["disabled-tool"]},
            "approval_required": True,
            "reasons": ["not_in_catalog", "disabled_observed_entry"],
        }
    )


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
