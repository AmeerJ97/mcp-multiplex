from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from mcp_multiplex.schemas import (
    Approval,
    AuditEvent,
    CatalogCandidate,
    CatalogEntry,
    HealthPayload,
    ObservedEntry,
    RemediationPlan,
    RuntimeBackend,
    ValidationError,
)


def observed_entry_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "observed_entry_id": "obs_context7",
        "agent_id": "agent_codex_user_default",
        "agent_kind": "codex",
        "config_path": "~/.codex/config.toml",
        "container_path": ["mcp_servers", "context7"],
        "mount_name": "context7",
        "enabled": True,
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "url": None,
        "headers_present": [],
        "env_names": [],
        "cwd": None,
        "tool_filters": {"enabled_tools": None, "disabled_tools": []},
        "approval_policy": None,
        "entry_hash": "sha256:abcdef",
        "raw_shape": "codex-toml",
        "parser_confidence": "complete",
    }


def catalog_entry_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "catalog_id": "srv_context7",
        "name": "context7",
        "canonical_name": "upstash.context7",
        "family_id": "context7",
        "variant_name": "official_npm",
        "display_label": "Context7",
        "aliases": ["context7-mcp", "@upstash/context7-mcp"],
        "review_state": "approved",
        "lifecycle_state": "enabled",
        "risk_tier": "normal",
        "provenance": [],
        "transport": {
            "frontend": "streamable_http",
            "hub_path": "/servers/context7/mcp",
            "backend": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@upstash/context7-mcp"],
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
        "active_set": {
            "eligible_profiles": ["coding-default", "docs"],
            "default_enabled": False,
        },
    }


def candidate_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate_id": "cand_new_server",
        "source": "observed_agent_config",
        "observed_entry_id": "obs_context7",
        "proposed_name": "new-server",
        "classification": "unknown_stdio",
        "review_state": "pending",
        "risk_tier": "unknown",
        "confidence": "low",
        "backend_shape": {"type": "stdio", "command": "uvx", "args": ["some-mcp-server"]},
        "approval_required": True,
        "reasons": ["unknown_package", "not_in_catalog"],
    }


def remediation_plan_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "plan_id": "plan_rewrite_context7",
        "plan_type": "rewrite_known_direct",
        "status": "pending_approval",
        "agent_id": "agent_codex_user_default",
        "target_path": "~/.codex/config.toml",
        "observed_entry_id": "obs_context7",
        "catalog_id": "srv_context7",
        "policy": {
            "auto_apply_allowed": False,
            "approval_required": True,
            "approval_reason": "project_shared_config",
        },
        "diff": {"format": "unified", "text": "--- before\n+++ after\n"},
        "expected_preimage_hash": "sha256:abcdef",
        "rollback_strategy": "restore_backup",
        "risk": {"tier": "normal", "reasons": []},
        "created_at": "2026-06-20T00:00:00Z",
    }


def approval_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "approval_id": "appr_rewrite_context7",
        "plan_id": "plan_rewrite_context7",
        "state": "approved",
        "actor": "local_operator",
        "channel": "tui",
        "created_at": "2026-06-20T00:00:00Z",
        "expires_at": "2026-06-20T01:00:00Z",
        "decision_at": "2026-06-20T00:05:00Z",
        "comment": "Approved known rewrite",
    }


def audit_event_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": "evt_rewrite_applied",
        "event_type": "remediation.applied",
        "actor": "daemon",
        "agent_id": "agent_codex_user_default",
        "plan_id": "plan_rewrite_context7",
        "target_path": "~/.codex/config.toml",
        "before_hash": "sha256:before",
        "after_hash": "sha256:after",
        "backup_id": "bak_context7",
        "result": "success",
        "timestamp": "2026-06-20T00:00:00Z",
        "redaction": "secret_values_removed",
        "previous_event_hash": "sha256:previous",
        "event_hash": "sha256:event",
    }


def runtime_backend_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "backend_id": "be_context7_global",
        "catalog_id": "srv_context7",
        "runtime_pool_key": "global:context7",
        "state": "hot",
        "pid": 12345,
        "account_scope": "none",
        "workspace_root": None,
        "backend_initialize_count": 1,
        "frontend_session_count": 2,
        "created_at": "2026-06-20T00:00:00Z",
        "last_used_at": "2026-06-20T00:05:00Z",
    }


def health_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "MCPMultiplexHealth",
        "ok": False,
        "summary": {
            "agents": 5,
            "blockers": 1,
            "warnings": 0,
            "notices": 0,
            "active_servers": 12,
            "hot_backends": 4,
            "pending_approvals": 2,
        },
        "blockers": [
            {
                "area": "compliance",
                "code": "active_direct_bypass",
                "agent_id": "agent_codex_user_default",
                "server": "context7",
                "detail": "Codex has an active direct stdio entry for context7",
            }
        ],
        "warnings": [],
        "notices": [],
    }


@pytest.mark.parametrize(
    ("model", "payload_factory"),
    [
        (ObservedEntry, observed_entry_payload),
        (CatalogEntry, catalog_entry_payload),
        (CatalogCandidate, candidate_payload),
        (RemediationPlan, remediation_plan_payload),
        (Approval, approval_payload),
        (AuditEvent, audit_event_payload),
        (RuntimeBackend, runtime_backend_payload),
        (HealthPayload, health_payload),
    ],
)
def test_schema_models_round_trip_stably(
    model: Any,
    payload_factory: Callable[[], dict[str, object]],
) -> None:
    payload = payload_factory()

    parsed = model.from_dict(payload)

    assert parsed.to_dict() == payload
    assert model.from_dict(parsed.to_dict()).to_dict() == payload


def test_required_fields_are_enforced() -> None:
    payload = observed_entry_payload()
    del payload["mount_name"]

    with pytest.raises(ValidationError, match="mount_name is required"):
        ObservedEntry.from_dict(payload)


def test_schema_version_is_enforced() -> None:
    payload = health_payload()
    payload["schema_version"] = 2

    with pytest.raises(ValidationError, match="schema_version must be 1"):
        HealthPayload.from_dict(payload)


def test_unknown_fields_are_rejected() -> None:
    payload = runtime_backend_payload()
    payload["raw_secret"] = "do-not-accept"

    with pytest.raises(ValidationError, match="unknown fields"):
        RuntimeBackend.from_dict(payload)


def test_unknown_candidates_must_require_approval() -> None:
    payload = candidate_payload()
    payload["approval_required"] = False

    with pytest.raises(ValidationError, match="unknown candidates must require approval"):
        CatalogCandidate.from_dict(payload)


def test_known_catalog_entry_requires_hub_path() -> None:
    payload = catalog_entry_payload()
    transport = payload["transport"]
    assert isinstance(transport, dict)
    transport["hub_path"] = "http://127.0.0.1:30000/servers/context7/mcp"

    with pytest.raises(ValidationError, match="transport.hub_path"):
        CatalogEntry.from_dict(payload)


def test_remediation_plan_approval_requires_reason() -> None:
    payload = remediation_plan_payload()
    policy = payload["policy"]
    assert isinstance(policy, dict)
    del policy["approval_reason"]

    with pytest.raises(ValidationError, match="approval_required plans need approval_reason"):
        RemediationPlan.from_dict(payload)


def test_audit_event_requires_redaction_contract() -> None:
    payload = audit_event_payload()
    payload["redaction"] = "none"

    with pytest.raises(ValidationError, match="secret value redaction"):
        AuditEvent.from_dict(payload)


def test_health_summary_counts_must_match_issue_lists() -> None:
    payload = health_payload()
    summary = payload["summary"]
    assert isinstance(summary, dict)
    summary["blockers"] = 0

    with pytest.raises(ValidationError, match="summary.blockers"):
        HealthPayload.from_dict(payload)


def test_ok_health_payload_cannot_contain_blockers() -> None:
    payload = health_payload()
    payload["ok"] = True

    with pytest.raises(ValidationError, match="ok health payloads cannot contain blockers"):
        HealthPayload.from_dict(payload)
