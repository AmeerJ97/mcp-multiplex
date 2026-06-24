from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.approvals import ApprovalError, ApprovalStore
from mcp_multiplex.cli import main as cli_main
from mcp_multiplex.observability import EventStore
from mcp_multiplex.storage import connect, migrate

CREATED_AT = "2026-06-20T00:00:00Z"
DECISION_AT = "2026-06-20T00:05:00Z"


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "multiplex.db")
    migrate(connection)
    insert_plan(connection)
    return connection


def insert_plan(
    connection: sqlite3.Connection,
    *,
    plan_id: str = "plan_rewrite_context7",
    status: str = "pending_approval",
    approval_required: bool = True,
) -> None:
    connection.execute(
        """
        INSERT INTO agents (agent_id, agent_kind, display_name)
        VALUES ('agent_codex_user_default', 'codex', 'Codex CLI')
        ON CONFLICT(agent_id) DO NOTHING
        """
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
        VALUES (?, 1, 'rewrite_known_direct', ?, 'agent_codex_user_default',
                '/tmp/config.toml', NULL, NULL, ?, 'unified', '--- before\n+++ after\n',
                'sha256:preimage', 'restore_backup_before_apply', ?, ?)
        """,
        (
            plan_id,
            status,
            json.dumps(
                {
                    "approval_required": approval_required,
                    "approval_reason": "dry_run_review_required",
                }
            ),
            json.dumps({"tier": "normal"}),
            CREATED_AT,
        ),
    )
    connection.commit()


def test_create_pending_approval_is_idempotent(connection: sqlite3.Connection) -> None:
    store = ApprovalStore(connection)

    first = store.create_pending("plan_rewrite_context7", created_at=CREATED_AT)
    second = store.create_pending("plan_rewrite_context7", created_at=CREATED_AT)

    assert first == second
    assert first.approval_id.startswith("appr_")
    assert first.state == "pending"
    assert first.actor == "daemon"
    assert first.channel == "planner"
    assert [approval.approval_id for approval in store.list()] == [first.approval_id]


def test_approve_pending_plan_records_decision_event_and_updates_plan(
    connection: sqlite3.Connection,
) -> None:
    store = ApprovalStore(connection)
    pending = store.create_pending("plan_rewrite_context7", created_at=CREATED_AT)

    approved = store.approve(
        pending.approval_id,
        actor="local_operator",
        channel="cli",
        decided_at=DECISION_AT,
        comment="Looks correct",
    )

    assert approved.state == "approved"
    assert approved.actor == "local_operator"
    assert approved.channel == "cli"
    assert approved.decision_at == DECISION_AT
    assert approved.comment == "Looks correct"
    plan_status = connection.execute(
        "SELECT status FROM remediation_plans WHERE plan_id = 'plan_rewrite_context7'"
    ).fetchone()["status"]
    assert plan_status == "approved"
    events = EventStore(connection).query(event_type="approval.approved")
    assert len(events) == 1
    assert events[0].event.plan_id == "plan_rewrite_context7"
    assert events[0].payload["approval_id"] == pending.approval_id
    assert events[0].payload["comment"] == "Looks correct"


def test_reject_pending_plan_records_decision_event_and_blocks_reapproval(
    connection: sqlite3.Connection,
) -> None:
    store = ApprovalStore(connection)
    pending = store.create_pending("plan_rewrite_context7", created_at=CREATED_AT)

    rejected = store.reject(
        pending.approval_id,
        actor="local_operator",
        channel="cli",
        decided_at=DECISION_AT,
        comment="Wrong target",
    )

    assert rejected.state == "rejected"
    plan_status = connection.execute(
        "SELECT status FROM remediation_plans WHERE plan_id = 'plan_rewrite_context7'"
    ).fetchone()["status"]
    assert plan_status == "rejected"
    with pytest.raises(ApprovalError, match="not pending"):
        store.approve(pending.approval_id, actor="local_operator", channel="cli")


def test_model_only_channel_cannot_decide_destructive_approval(
    connection: sqlite3.Connection,
) -> None:
    store = ApprovalStore(connection)
    pending = store.create_pending("plan_rewrite_context7", created_at=CREATED_AT)

    with pytest.raises(ApprovalError, match="model-only approval is insufficient"):
        store.approve(pending.approval_id, actor="agent", channel="control_mcp")


def test_not_required_approval_only_for_plan_policy_that_does_not_require_approval(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path / "multiplex.db")
    migrate(connection)
    insert_plan(connection, approval_required=False, status="draft")

    approval = ApprovalStore(connection).create_not_required(
        "plan_rewrite_context7",
        created_at=CREATED_AT,
    )

    assert approval.state == "not_required"
    assert approval.decision_at is None
    assert (
        connection.execute(
            "SELECT status FROM remediation_plans WHERE plan_id = 'plan_rewrite_context7'"
        ).fetchone()["status"]
        == "draft"
    )


def test_missing_plan_cannot_create_approval(connection: sqlite3.Connection) -> None:
    with pytest.raises(ApprovalError, match="unknown plan"):
        ApprovalStore(connection).create_pending("plan_missing", created_at=CREATED_AT)


def test_cli_approval_list_approve_and_reject(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    migrate(connection)
    insert_plan(connection)
    pending = ApprovalStore(connection).create_pending(
        "plan_rewrite_context7", created_at=CREATED_AT
    )

    exit_code = cli_main(["approval", "list", "--db-path", str(db_path)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexApprovalList"
    assert payload["approvals"][0]["approval_id"] == pending.approval_id
    assert payload["approvals"][0]["state"] == "pending"

    exit_code = cli_main(
        [
            "approval",
            "approve",
            pending.approval_id,
            "--db-path",
            str(db_path),
            "--actor",
            "local_operator",
            "--comment",
            "approved from cli",
        ]
    )

    assert exit_code == 0
    approved_payload = json.loads(capsys.readouterr().out)
    assert approved_payload["kind"] == "MCPMultiplexApprovalDecision"
    assert approved_payload["approval"]["state"] == "approved"
    assert approved_payload["plan_status"] == "approved"

    insert_plan(connection, plan_id="plan_second", status="pending_approval")
    second = ApprovalStore(connection).create_pending("plan_second", created_at=CREATED_AT)
    exit_code = cli_main(
        [
            "approval",
            "reject",
            second.approval_id,
            "--db-path",
            str(db_path),
            "--actor",
            "local_operator",
        ]
    )

    assert exit_code == 0
    rejected_payload = json.loads(capsys.readouterr().out)
    assert rejected_payload["approval"]["state"] == "rejected"
    assert rejected_payload["plan_status"] == "rejected"
