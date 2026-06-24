from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mcp_multiplex.adapters import (
    AgentRegistry,
    parse_cline_config,
    parse_codex_config,
    parse_opencode_config,
)
from mcp_multiplex.apply import (
    ApplyError,
    ConfigBackupStore,
    apply_plan,
    rollback_backup,
    sha256_bytes,
)
from mcp_multiplex.approvals import ApprovalStore
from mcp_multiplex.catalog import match_observed_entry
from mcp_multiplex.observability import EventStore, ObservedEntryStore
from mcp_multiplex.planning import generate_known_direct_rewrite_plan
from mcp_multiplex.schemas import CatalogEntry, RemediationPlan
from mcp_multiplex.storage import connect
from tests.test_catalog_matching import catalog_entry

CREATED_AT = "2026-06-20T00:00:00Z"
APPROVED_AT = "2026-06-20T00:01:00Z"
APPLIED_AT = "2026-06-20T00:02:00Z"


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "multiplex.db")
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    return connection


def test_approved_known_direct_plan_applies_atomically_with_backup(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    config_path = _write_direct_config(tmp_path)
    original_bytes = config_path.read_bytes()
    plan = _store_plan(connection, config_path)
    pending = ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)
    ApprovalStore(connection).approve(
        pending.approval_id,
        actor="local_operator",
        channel="cli",
        decided_at=APPROVED_AT,
    )

    result = apply_plan(
        connection,
        plan.plan_id,
        backup_dir=tmp_path / "backups",
        timestamp=APPLIED_AT,
    )

    assert result.verified is True
    assert result.before_hash == sha256_bytes(original_bytes)
    assert Path(result.backup.backup_path).read_bytes() == original_bytes
    assert result.backup.bytes == len(original_bytes)
    assert ConfigBackupStore(connection).list(plan_id=plan.plan_id) == [result.backup]
    rewritten = config_path.read_text(encoding="utf-8")
    assert rewritten == (
        '[mcp_servers.context7]\nurl = "http://127.0.0.1:30000/servers/context7/mcp"\n\n'
    )
    parsed = parse_codex_config(config_path).observed_entries[0]
    assert parsed.transport == "streamable_http"
    assert parsed.command is None
    assert parsed.args == []
    assert parsed.url == "http://127.0.0.1:30000/servers/context7/mcp"
    assert _plan_status(connection, plan.plan_id) == "applied"
    events = EventStore(connection).query()
    assert [event.event.event_type for event in events] == [
        "approval.approved",
        "remediation.applied",
    ]
    assert events[-1].event.backup_id == result.backup.backup_id
    assert events[-1].event.before_hash == result.before_hash
    assert events[-1].event.after_hash == result.after_hash


def test_approval_required_plan_cannot_apply_without_approval(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    config_path = _write_direct_config(tmp_path)
    original_bytes = config_path.read_bytes()
    plan = _store_plan(connection, config_path)
    ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)

    with pytest.raises(ApplyError, match="cannot apply without approval"):
        apply_plan(connection, plan.plan_id, backup_dir=tmp_path / "backups", timestamp=APPLIED_AT)

    assert config_path.read_bytes() == original_bytes
    assert ConfigBackupStore(connection).list(plan_id=plan.plan_id) == []
    assert _plan_status(connection, plan.plan_id) == "pending_approval"


def test_rejected_plan_cannot_apply(connection: sqlite3.Connection, tmp_path: Path) -> None:
    config_path = _write_direct_config(tmp_path)
    plan = _store_plan(connection, config_path)
    pending = ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)
    ApprovalStore(connection).reject(
        pending.approval_id,
        actor="local_operator",
        channel="cli",
        decided_at=APPROVED_AT,
    )

    with pytest.raises(ApplyError, match="rejected plan cannot apply"):
        apply_plan(connection, plan.plan_id, backup_dir=tmp_path / "backups", timestamp=APPLIED_AT)


def test_preimage_hash_mismatch_blocks_before_backup(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    config_path = _write_direct_config(tmp_path)
    plan = _store_plan(connection, config_path)
    pending = ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)
    ApprovalStore(connection).approve(
        pending.approval_id,
        actor="local_operator",
        channel="cli",
        decided_at=APPROVED_AT,
    )
    config_path.write_text(
        '[mcp_servers.context7]\ncommand = "uvx"\nargs = ["different"]\n',
        encoding="utf-8",
    )
    changed_bytes = config_path.read_bytes()

    with pytest.raises(ApplyError, match="expected pre-image hash"):
        apply_plan(connection, plan.plan_id, backup_dir=tmp_path / "backups", timestamp=APPLIED_AT)

    assert config_path.read_bytes() == changed_bytes
    assert ConfigBackupStore(connection).list(plan_id=plan.plan_id) == []


def test_post_write_validation_failure_rolls_back_exact_bytes(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    config_path = _write_direct_config(tmp_path)
    original_bytes = config_path.read_bytes()
    plan = _store_plan(connection, config_path)
    pending = ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)
    ApprovalStore(connection).approve(
        pending.approval_id,
        actor="local_operator",
        channel="cli",
        decided_at=APPROVED_AT,
    )

    def fail_post_write(path: Path) -> None:
        assert "http://127.0.0.1:30000/servers/context7/mcp" in path.read_text()
        raise RuntimeError("injected validation failure")

    with pytest.raises(ApplyError, match="rollback completed"):
        apply_plan(
            connection,
            plan.plan_id,
            backup_dir=tmp_path / "backups",
            timestamp=APPLIED_AT,
            post_write_validator=fail_post_write,
        )

    assert config_path.read_bytes() == original_bytes
    backup = ConfigBackupStore(connection).list(plan_id=plan.plan_id)[0]
    assert backup.restored_at == APPLIED_AT
    assert sha256_bytes(config_path.read_bytes()) == backup.before_hash
    assert _plan_status(connection, plan.plan_id) == "failed"
    assert [event.event.event_type for event in EventStore(connection).query()] == [
        "approval.approved",
        "rollback.completed",
        "remediation.failed",
    ]


def test_manual_rollback_restores_backup_bytes_after_successful_apply(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    config_path = _write_direct_config(tmp_path)
    original_bytes = config_path.read_bytes()
    plan = _store_plan(connection, config_path)
    pending = ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)
    ApprovalStore(connection).approve(
        pending.approval_id,
        actor="local_operator",
        channel="cli",
        decided_at=APPROVED_AT,
    )
    result = apply_plan(
        connection,
        plan.plan_id,
        backup_dir=tmp_path / "backups",
        timestamp=APPLIED_AT,
    )

    rollback = rollback_backup(
        connection,
        result.backup.backup_id,
        actor="local_operator",
        timestamp="2026-06-20T00:03:00Z",
    )

    assert config_path.read_bytes() == original_bytes
    assert rollback.restored_hash == result.before_hash
    assert rollback.backup.restored_at == "2026-06-20T00:03:00Z"
    assert EventStore(connection).query(event_type="rollback.completed")[-1].event.backup_id == (
        result.backup.backup_id
    )


def test_cline_known_direct_rewrite_uses_native_transport_and_preserves_fields(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    AgentRegistry(connection).create(
        agent_id="agent_cline_user_default",
        agent_kind="cline",
        display_name="Cline",
    )
    config_path = tmp_path / "cline_mcp_settings.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "context7": {
                        "command": "npx",
                        "args": ["-y", "@upstash/context7-mcp"],
                        "disabled": False,
                        "autoApprove": ["ping"],
                    }
                }
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    observed = parse_cline_config(config_path).observed_entries[0]
    ObservedEntryStore(connection).upsert_many([observed])
    catalog = catalog_entry()
    match = match_observed_entry(observed, [catalog])
    plan = generate_known_direct_rewrite_plan(
        observed,
        CatalogEntry.from_dict(catalog.to_dict()),
        match,
        created_at=CREATED_AT,
    )
    _insert_plan(connection, plan)
    pending = ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)
    ApprovalStore(connection).approve(
        pending.approval_id,
        actor="local_operator",
        channel="cli",
        decided_at=APPROVED_AT,
    )

    apply_plan(connection, plan.plan_id, backup_dir=tmp_path / "backups", timestamp=APPLIED_AT)

    rewritten = json.loads(config_path.read_text(encoding="utf-8"))
    assert rewritten["mcpServers"]["context7"] == {
        "autoApprove": ["ping"],
        "disabled": False,
        "transport": {
            "type": "streamableHttp",
            "url": "http://127.0.0.1:30000/servers/context7/mcp",
        },
    }
    parsed = parse_cline_config(config_path).observed_entries[0]
    assert parsed.command is None
    assert parsed.url == "http://127.0.0.1:30000/servers/context7/mcp"


def test_opencode_known_direct_rewrite_uses_native_remote_and_preserves_enabled(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    AgentRegistry(connection).create(
        agent_id="agent_opencode_user_default",
        agent_kind="opencode",
        display_name="OpenCode",
    )
    config_path = tmp_path / "opencode.jsonc"
    config_path.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {
                    "context7": {
                        "type": "local",
                        "command": ["npx", "-y", "@upstash/context7-mcp"],
                        "enabled": True,
                    }
                },
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    observed = parse_opencode_config(config_path).observed_entries[0]
    ObservedEntryStore(connection).upsert_many([observed])
    catalog = catalog_entry()
    match = match_observed_entry(observed, [catalog])
    plan = generate_known_direct_rewrite_plan(
        observed,
        CatalogEntry.from_dict(catalog.to_dict()),
        match,
        created_at=CREATED_AT,
    )
    _insert_plan(connection, plan)
    pending = ApprovalStore(connection).create_pending(plan.plan_id, created_at=CREATED_AT)
    ApprovalStore(connection).approve(
        pending.approval_id,
        actor="local_operator",
        channel="cli",
        decided_at=APPROVED_AT,
    )

    apply_plan(connection, plan.plan_id, backup_dir=tmp_path / "backups", timestamp=APPLIED_AT)

    rewritten = json.loads(config_path.read_text(encoding="utf-8"))
    assert rewritten["mcp"]["context7"] == {
        "enabled": True,
        "type": "remote",
        "url": "http://127.0.0.1:30000/servers/context7/mcp",
    }
    parsed = parse_opencode_config(config_path).observed_entries[0]
    assert parsed.command is None
    assert parsed.url == "http://127.0.0.1:30000/servers/context7/mcp"


def _write_direct_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "@upstash/context7-mcp"]\n',
        encoding="utf-8",
    )
    return config_path


def _store_plan(connection: sqlite3.Connection, config_path: Path) -> RemediationPlan:
    observed = parse_codex_config(config_path).observed_entries[0]
    ObservedEntryStore(connection).upsert_many([observed])
    catalog = catalog_entry()
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
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.plan_id,
                plan.schema_version,
                plan.plan_type,
                plan.status,
                plan.agent_id,
                plan.target_path,
                plan.observed_entry_id,
                json.dumps(plan.policy, sort_keys=True),
                plan.diff.format,
                plan.diff.text,
                plan.expected_preimage_hash,
                plan.rollback_strategy,
                json.dumps(plan.risk, sort_keys=True),
                plan.created_at,
            ),
        )


def _plan_status(connection: sqlite3.Connection, plan_id: str) -> Any:
    return connection.execute(
        "SELECT status FROM remediation_plans WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()["status"]
