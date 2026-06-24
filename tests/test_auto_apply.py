from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.adapters import AgentConfigPath, AgentRegistry, parse_codex_config
from mcp_multiplex.apply import ApplyError, auto_apply_plan, evaluate_auto_apply
from mcp_multiplex.approvals import ApprovalStore
from mcp_multiplex.catalog import match_observed_entry
from mcp_multiplex.observability import EventStore, ObservedEntryStore
from mcp_multiplex.planning import generate_known_direct_rewrite_plan
from mcp_multiplex.schemas import CatalogEntry, RemediationPlan
from mcp_multiplex.storage import connect
from tests.test_catalog_matching import catalog_entry

CREATED_AT = "2026-06-20T00:00:00Z"
AUTO_APPLIED_AT = "2026-06-20T00:02:00Z"


def test_certified_safe_known_direct_plan_auto_applies(tmp_path: Path) -> None:
    config_path = _write_direct_config(tmp_path)
    connection = _connection_for_config(tmp_path, config_path, certification_level="certified")
    original_bytes = config_path.read_bytes()
    plan = _store_plan(connection, config_path)

    decision = evaluate_auto_apply(connection, plan.plan_id)
    result = auto_apply_plan(
        connection,
        plan.plan_id,
        backup_dir=tmp_path / "backups",
        timestamp=AUTO_APPLIED_AT,
    )

    assert decision.eligible is True
    assert decision.reasons == ["certified_safe_known_direct"]
    assert result.before_hash.startswith("sha256:")
    assert Path(result.backup.backup_path).read_bytes() == original_bytes
    assert config_path.read_text(encoding="utf-8") == (
        '[mcp_servers.context7]\nurl = "http://127.0.0.1:30000/servers/context7/mcp"\n\n'
    )
    approval = ApprovalStore(connection).find_by_plan(plan.plan_id)
    assert approval is not None
    assert approval.state == "not_required"
    assert approval.channel == "auto_policy"
    assert _plan_status(connection, plan.plan_id) == "applied"
    assert [event.event.event_type for event in EventStore(connection).query()] == [
        "auto_apply.authorized",
        "remediation.applied",
    ]


def test_auto_apply_rejects_uncertified_agent_without_mutating(tmp_path: Path) -> None:
    config_path = _write_direct_config(tmp_path)
    connection = _connection_for_config(tmp_path, config_path, certification_level="best_effort")
    original_bytes = config_path.read_bytes()
    plan = _store_plan(connection, config_path)

    decision = evaluate_auto_apply(connection, plan.plan_id)
    with pytest.raises(ApplyError, match="agent_not_certified"):
        auto_apply_plan(connection, plan.plan_id, timestamp=AUTO_APPLIED_AT)

    assert decision.eligible is False
    assert "agent_not_certified:best_effort" in decision.reasons
    assert config_path.read_bytes() == original_bytes
    assert _plan_status(connection, plan.plan_id) == "pending_approval"
    assert (
        EventStore(connection).query(event_type="auto_apply.rejected")[-1].payload["reasons"]
        == decision.reasons
    )


def test_auto_apply_rejects_project_shared_config(tmp_path: Path) -> None:
    config_path = _write_direct_config(tmp_path)
    connection = _connection_for_config(
        tmp_path,
        config_path,
        certification_level="certified",
        is_project_shared=True,
    )
    plan = _store_plan(connection, config_path)

    decision = evaluate_auto_apply(connection, plan.plan_id)

    assert decision.eligible is False
    assert "target_path_missing_or_project_shared" in decision.reasons


def test_auto_apply_rejects_env_or_cwd_ambiguity(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[mcp_servers.context7]",
                'command = "npx"',
                'args = ["-y", "@upstash/context7-mcp"]',
                'cwd = "/tmp/workspace"',
                "[mcp_servers.context7.env]",
                'TOKEN = "secretref:not-a-secret-value"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    connection = _connection_for_config(tmp_path, config_path, certification_level="certified")
    plan = _store_plan(connection, config_path)

    decision = evaluate_auto_apply(connection, plan.plan_id)

    assert decision.eligible is False
    assert "cwd_ambiguity" in decision.reasons
    assert "env_ambiguity" in decision.reasons


def test_auto_apply_rejects_non_exact_or_weak_policy_metadata(tmp_path: Path) -> None:
    config_path = _write_direct_config(tmp_path)
    connection = _connection_for_config(tmp_path, config_path, certification_level="certified")
    plan = _store_plan(connection, config_path)
    policy = dict(plan.policy)
    policy["reasons"] = ["known_direct_backend_match", "match_confidence:weak"]
    with connection:
        connection.execute(
            "UPDATE remediation_plans SET policy_json = ? WHERE plan_id = ?",
            (json.dumps(policy, sort_keys=True), plan.plan_id),
        )

    decision = evaluate_auto_apply(connection, plan.plan_id)

    assert decision.eligible is False
    assert "policy_match_confidence_not_high" in decision.reasons
    assert "policy_missing_exact_backend_fingerprint" in decision.reasons


def test_auto_apply_rewrite_loop_guard_rejects_repeated_mutation(tmp_path: Path) -> None:
    config_path = _write_direct_config(tmp_path)
    connection = _connection_for_config(tmp_path, config_path, certification_level="certified")
    plan = _store_plan(connection, config_path)
    auto_apply_plan(
        connection,
        plan.plan_id,
        backup_dir=tmp_path / "backups",
        timestamp=AUTO_APPLIED_AT,
    )

    decision = evaluate_auto_apply(connection, plan.plan_id)

    assert decision.eligible is False
    assert "plan_status_not_pending_approval:applied" in decision.reasons
    assert "rewrite_loop_guard_backup_exists" in decision.reasons
    assert "rewrite_loop_guard_prior_mutation_event" in decision.reasons


def _write_direct_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "@upstash/context7-mcp"]\n',
        encoding="utf-8",
    )
    return config_path


def _connection_for_config(
    tmp_path: Path,
    config_path: Path,
    *,
    certification_level: str,
    is_project_shared: bool = False,
) -> sqlite3.Connection:
    connection = connect(tmp_path / "multiplex.db")
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
        config_paths=[
            AgentConfigPath(
                path=str(config_path),
                format="toml",
                is_project_shared=is_project_shared,
            )
        ],
        certification_level=certification_level,
    )
    return connection


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


def _plan_status(connection: sqlite3.Connection, plan_id: str) -> str:
    return str(
        connection.execute(
            "SELECT status FROM remediation_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()["status"]
    )
