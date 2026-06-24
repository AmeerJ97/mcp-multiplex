from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.adapters import AgentRegistry, parse_codex_config
from mcp_multiplex.catalog import (
    CatalogCandidateStore,
    CatalogStore,
    candidate_from_observed_entry,
    match_observed_entry,
)
from mcp_multiplex.observability import EventStore
from mcp_multiplex.planning import (
    CONTROL_PLANE_CATALOG_ID,
    HUB_BASE_URL,
    PlanningError,
    generate_known_direct_rewrite_plan,
    generate_missing_control_plane_plan,
    generate_unknown_import_plan,
    generate_unsafe_local_http_plan,
    plan_self_healing_dry_run,
)
from mcp_multiplex.schemas import CatalogCandidate, ObservedEntry, RemediationPlan
from mcp_multiplex.storage import connect
from tests.test_candidate_staging import unknown_local_http_entry, unknown_stdio_entry
from tests.test_catalog_matching import catalog_entry, direct_context7

CREATED_AT = "2026-06-20T00:00:00Z"
FIXTURE_DIR = Path("tests/fixtures/agents/codex")


def test_known_direct_rewrite_plan_is_deterministic_dry_run_with_exact_diff(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    original_config = '[mcp_servers.context7]\ncommand = "npx"\n'
    config_path.write_text(original_config)
    observed = _with_config_path(direct_context7(), config_path)
    catalog = catalog_entry()
    match = match_observed_entry(observed, [catalog])

    first = generate_known_direct_rewrite_plan(
        observed,
        catalog,
        match,
        created_at=CREATED_AT,
    )
    second = generate_known_direct_rewrite_plan(
        observed,
        catalog,
        match,
        created_at=CREATED_AT,
    )

    assert first == second
    assert RemediationPlan.from_dict(first.to_dict()) == first
    assert config_path.read_text() == original_config
    assert first.plan_type == "rewrite_known_direct"
    assert first.status == "pending_approval"
    assert first.agent_id == observed.agent_id
    assert first.target_path == str(config_path)
    assert first.observed_entry_id == observed.observed_entry_id
    assert first.catalog_id == "srv_context7"
    assert first.expected_preimage_hash == observed.entry_hash
    assert first.policy["approval_required"] is True
    assert first.policy["approval_reason"] == "dry_run_review_required"
    assert first.policy["auto_apply_allowed"] is False
    assert first.policy["dry_run_only"] is True
    assert first.policy["before"]["target_path"] == str(config_path)
    assert first.policy["after"]["url"] == f"{HUB_BASE_URL}/servers/context7/mcp"
    assert first.diff.format == "unified"
    assert "--- before" in first.diff.text
    assert "+++ after" in first.diff.text
    assert '-  "transport": "stdio"' in first.diff.text
    assert '+  "transport": "streamable_http"' in first.diff.text
    assert '+  "url": "http://127.0.0.1:30000/servers/context7/mcp"' in first.diff.text
    assert first.risk["verification"] == "parse_config_and_confirm_hub_routed_entry"
    assert first.rollback_strategy == "restore_backup_before_apply"


def test_known_direct_rewrite_requires_high_confidence_routable_match() -> None:
    observed = unknown_stdio_entry()
    catalog = catalog_entry()
    match = match_observed_entry(observed, [catalog])

    with pytest.raises(PlanningError, match="does not identify"):
        generate_known_direct_rewrite_plan(
            observed,
            catalog,
            match,
            created_at=CREATED_AT,
        )


def test_unknown_import_plan_stages_candidate_without_mutating_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    original_config = '[mcp_servers.new-server]\ncommand = "uvx"\n'
    config_path.write_text(original_config)
    observed = _with_config_path(unknown_stdio_entry(enabled=False), config_path)
    candidate = _candidate(observed)

    plan = generate_unknown_import_plan(observed, candidate, created_at=CREATED_AT)

    assert config_path.read_text() == original_config
    assert plan.plan_type == "import_unknown_candidate"
    assert plan.status == "pending_approval"
    assert plan.target_path == str(config_path)
    assert plan.catalog_id.startswith("srv_candidate_new_server_")
    assert plan.policy["approval_reason"] == "unknown_candidate_review_required"
    assert plan.policy["auto_apply_allowed"] is False
    assert plan.policy["before"]["mount_name"] == "new-server"
    assert plan.policy["after"]["catalog_entry"]["review_state"] == "pending"
    assert plan.policy["after"]["catalog_entry"]["lifecycle_state"] == "disabled"
    assert "unknown_package" in plan.policy["reasons"]
    assert "classification:unknown_stdio" in plan.policy["reasons"]
    assert plan.risk["tier"] == "unknown"
    assert plan.risk["verification"] == "operator_reviews_candidate_before_catalog_import"
    assert plan.rollback_strategy == "delete_pending_candidate_before_apply"
    assert '+    "review_state": "pending"' in plan.diff.text


def test_unsafe_local_http_plan_is_high_risk_and_review_required(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    original_config = '{"mcpServers":{"local-http":{"url":"http://127.0.0.1:4567/mcp"}}}'
    config_path.write_text(original_config)
    observed = _with_config_path(unknown_local_http_entry(), config_path)
    candidate = _candidate(observed)

    plan = generate_unsafe_local_http_plan(observed, candidate, created_at=CREATED_AT)

    assert config_path.read_text() == original_config
    assert plan.plan_type == "unsafe_local_http_detected"
    assert plan.target_path == str(config_path)
    assert plan.policy["approval_required"] is True
    assert plan.policy["approval_reason"] == "unsafe_local_http_candidate"
    assert plan.policy["auto_apply_allowed"] is False
    assert "loopback_http_endpoint_cannot_be_auto_trusted" in plan.policy["reasons"]
    assert plan.risk["tier"] == "high"
    assert "local_endpoint_may_be_unmanaged_process" in plan.risk["reasons"]
    assert plan.risk["verification"] == "operator_confirms_process_owner_before_any_route"
    assert plan.rollback_strategy == "no_config_mutation_performed"
    assert "required_action" in plan.diff.text


def test_unknown_import_rejects_local_http_candidate() -> None:
    observed = unknown_local_http_entry()
    candidate = _candidate(observed)

    with pytest.raises(PlanningError, match="unsafe local HTTP"):
        generate_unknown_import_plan(observed, candidate, created_at=CREATED_AT)


def test_missing_control_plane_plan_has_synthetic_observation_and_rollback() -> None:
    plan = generate_missing_control_plane_plan(
        agent_id="agent_codex_user_default",
        target_path="~/.codex/config.toml",
        expected_preimage_hash="sha256:configpreimage",
        created_at=CREATED_AT,
    )

    assert plan.plan_type == "install_missing_control_plane"
    assert plan.status == "pending_approval"
    assert plan.agent_id == "agent_codex_user_default"
    assert plan.target_path == "~/.codex/config.toml"
    assert plan.observed_entry_id.startswith("obs_")
    assert plan.catalog_id == CONTROL_PLANE_CATALOG_ID
    assert plan.expected_preimage_hash == "sha256:configpreimage"
    assert plan.policy["approval_reason"] == "missing_control_plane_review_required"
    assert plan.policy["after"]["mcp_hub"] == {
        "transport": "streamable_http",
        "url": f"{HUB_BASE_URL}/servers/mcp_hub/mcp",
        "role": "control_plane",
        "auth": {
            "kind": "bearer_token_env_var",
            "env_var": "MCP_MULTIPLEX_CONTROL_TOKEN",
            "token_ref_required": True,
        },
        "headers_present": ["Authorization"],
    }
    assert plan.risk["verification"] == "parse_config_and_confirm_authenticated_mcp_hub_entry"
    assert plan.rollback_strategy == "restore_backup_before_apply"
    assert '-  "mcp_hub": null' in plan.diff.text
    assert '+  "mcp_hub": {' in plan.diff.text


def test_missing_control_plane_plan_uses_header_secret_ref_contract_for_non_codex() -> None:
    plan = generate_missing_control_plane_plan(
        agent_id="agent_claude_user_default",
        target_path="~/.claude.json",
        expected_preimage_hash="sha256:configpreimage",
        created_at=CREATED_AT,
    )

    assert plan.policy["after"]["mcp_hub"]["auth"] == {
        "kind": "authorization_header",
        "header": "Authorization",
        "value_source": "agent_token_secret_ref",
        "token_ref_required": True,
    }


def test_candidate_plan_requires_candidate_to_match_observed_entry() -> None:
    observed = unknown_stdio_entry()
    other_candidate = CatalogCandidate.from_dict(
        {
            **_candidate(observed).to_dict(),
            "candidate_id": "cand_other",
            "observed_entry_id": "obs_other",
        }
    )

    with pytest.raises(PlanningError, match="candidate does not belong"):
        generate_unknown_import_plan(observed, other_candidate, created_at=CREATED_AT)


def test_self_healing_pipeline_plans_known_rewrite_and_missing_control_plane(
    tmp_path: Path,
) -> None:
    connection = _connection_with_codex_agent(tmp_path)
    CatalogStore(connection).upsert(catalog_entry())
    config_path = tmp_path / "config.toml"
    original_config = '[mcp_servers.context7]\ncommand = "npx"\n'
    config_path.write_text(original_config)
    observed = _with_config_path(direct_context7(), config_path)

    result = plan_self_healing_dry_run(
        connection,
        [observed],
        run_id="run_pipeline_known",
        timestamp=CREATED_AT,
    )

    assert config_path.read_text() == original_config
    assert [plan.plan_type for plan in result.plans] == [
        "rewrite_known_direct",
        "install_missing_control_plane",
    ]
    assert [outcome.action for outcome in result.outcomes] == [
        "planned_known_direct_rewrite",
        "planned_missing_control_plane",
    ]
    assert result.ingestion.health["ok"] is False
    assert result.plans[0].policy["after"]["url"] == f"{HUB_BASE_URL}/servers/context7/mcp"
    assert result.plans[1].catalog_id == CONTROL_PLANE_CATALOG_ID
    assert result.plans[1].expected_preimage_hash.startswith("sha256:")
    assert [event.event.event_type for event in result.events] == [
        "config.observed",
        "config.drift_detected",
        "remediation.planned",
    ]
    remediation_event = result.events[-1]
    assert remediation_event.payload == {
        "plan_count": 2,
        "plan_ids": [plan.plan_id for plan in result.plans],
        "plan_types": ["rewrite_known_direct", "install_missing_control_plane"],
        "dry_run_only": True,
    }
    assert EventStore(connection).validate_hash_chain() == []


def test_self_healing_pipeline_skips_known_entry_already_hub_routed(
    tmp_path: Path,
) -> None:
    connection = _connection_with_codex_agent(tmp_path)
    CatalogStore(connection).upsert(catalog_entry())
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[mcp_servers.context7]\nurl = "http://127.0.0.1:30000/servers/context7/mcp"\n',
        encoding="utf-8",
    )
    observed = ObservedEntry.from_dict(
        {
            **_with_config_path(direct_context7(), config_path).to_dict(),
            "transport": "streamable_http",
            "command": None,
            "args": [],
            "url": "http://127.0.0.1:30000/servers/context7/mcp",
            "parser_confidence": "partial",
        }
    )

    result = plan_self_healing_dry_run(
        connection,
        [observed],
        run_id="run_pipeline_known_already_hub",
        timestamp=CREATED_AT,
        include_missing_control_plane=False,
    )

    assert result.plans == []
    assert result.outcomes[0].action == "already_compliant"
    assert result.outcomes[0].reason == "entry is already routed through the Hub"
    assert config_path.read_text(encoding="utf-8").startswith("[mcp_servers.context7]")


def test_self_healing_pipeline_stages_unknown_and_unsafe_candidates(tmp_path: Path) -> None:
    connection = _connection_with_codex_agent(tmp_path)
    stdio_path = tmp_path / "unknown.toml"
    local_http_path = tmp_path / "local.json"
    stdio_path.write_text('[mcp_servers.new-server]\ncommand = "uvx"\n')
    local_http_path.write_text('{"mcpServers":{"local-http":{"url":"http://127.0.0.1:4567/mcp"}}}')
    stdio_entry = _with_config_path(unknown_stdio_entry(), stdio_path)
    local_http_entry = _with_config_path(unknown_local_http_entry(), local_http_path)

    result = plan_self_healing_dry_run(
        connection,
        [local_http_entry, stdio_entry],
        run_id="run_pipeline_unknowns",
        timestamp=CREATED_AT,
        include_missing_control_plane=False,
    )

    assert stdio_path.read_text() == '[mcp_servers.new-server]\ncommand = "uvx"\n'
    assert local_http_path.read_text() == (
        '{"mcpServers":{"local-http":{"url":"http://127.0.0.1:4567/mcp"}}}'
    )
    assert [plan.plan_type for plan in result.plans] == [
        "unsafe_local_http_detected",
        "import_unknown_candidate",
    ]
    assert [outcome.action for outcome in result.outcomes] == [
        "planned_unsafe_local_http",
        "planned_unknown_import",
    ]
    assert [candidate.classification for candidate in CatalogCandidateStore(connection).list()] == [
        "unknown_local_http",
        "unknown_stdio",
    ]
    assert result.plans[0].policy["approval_reason"] == "unsafe_local_http_candidate"
    assert result.plans[1].policy["approval_reason"] == "unknown_candidate_review_required"


def test_self_healing_pipeline_blocks_unsupported_entries_without_plan(tmp_path: Path) -> None:
    connection = _connection_with_codex_agent(tmp_path)
    observed = parse_codex_config(FIXTURE_DIR / "unsupported-field.input.toml").observed_entries[0]
    observed = _with_config_path(observed, tmp_path / "config.toml")

    result = plan_self_healing_dry_run(
        connection,
        [observed],
        run_id="run_pipeline_unsupported",
        timestamp=CREATED_AT,
        include_missing_control_plane=False,
    )

    assert result.plans == []
    assert [outcome.action for outcome in result.outcomes] == ["blocked_unsupported_entry"]
    assert result.outcomes[0].reason == "parser confidence is incomplete; planning is audit-only"
    assert [event.event.event_type for event in result.events] == [
        "config.observed",
        "config.drift_detected",
    ]


def test_self_healing_pipeline_is_deterministic_across_empty_databases(tmp_path: Path) -> None:
    first = _connection_with_codex_agent(tmp_path / "first")
    second = _connection_with_codex_agent(tmp_path / "second")
    CatalogStore(first).upsert(catalog_entry())
    CatalogStore(second).upsert(catalog_entry())
    observed = _with_config_path(direct_context7(), tmp_path / "config.toml")

    first_result = plan_self_healing_dry_run(
        first,
        [observed],
        run_id="run_pipeline_deterministic",
        timestamp=CREATED_AT,
        emit_events=False,
    )
    second_result = plan_self_healing_dry_run(
        second,
        [observed],
        run_id="run_pipeline_deterministic",
        timestamp=CREATED_AT,
        emit_events=False,
    )

    assert [plan.to_dict() for plan in first_result.plans] == [
        plan.to_dict() for plan in second_result.plans
    ]
    assert [outcome.to_dict() for outcome in first_result.outcomes] == [
        outcome.to_dict() for outcome in second_result.outcomes
    ]


def _candidate(observed_entry: ObservedEntry) -> CatalogCandidate:
    candidate = candidate_from_observed_entry(observed_entry)
    assert candidate is not None
    return candidate


def _with_config_path(observed_entry: ObservedEntry, path: Path) -> ObservedEntry:
    return ObservedEntry.from_dict({**observed_entry.to_dict(), "config_path": str(path)})


def _connection_with_codex_agent(tmp_path: Path) -> sqlite3.Connection:
    tmp_path.mkdir(parents=True, exist_ok=True)
    connection = connect(tmp_path / "multiplex.db")
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    return connection
