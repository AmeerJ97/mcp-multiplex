"""Dry-run remediation plan generation."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from mcp_multiplex.catalog import (
    CatalogStore,
    match_observed_entry_from_store,
    stage_unknown_candidate,
)
from mcp_multiplex.catalog.candidates import CandidateStageResult
from mcp_multiplex.catalog.matching import CatalogMatch
from mcp_multiplex.observability import (
    EventRecord,
    EventStore,
    IngestionResult,
    ingest_observed_entries,
)
from mcp_multiplex.schemas import CatalogCandidate, CatalogEntry, ObservedEntry, RemediationPlan

HUB_BASE_URL = "http://127.0.0.1:30000"
CONTROL_PLANE_CATALOG_ID = "srv_mcp_hub_control_plane"


class PlanningError(ValueError):
    """Raised when a dry-run remediation plan would be unsafe or ambiguous."""


PlanKind = Literal[
    "rewrite_known_direct",
    "import_unknown_candidate",
    "unsafe_local_http_detected",
    "install_missing_control_plane",
]

PlanAction = Literal[
    "already_compliant",
    "planned_known_direct_rewrite",
    "planned_unknown_import",
    "planned_unsafe_local_http",
    "planned_missing_control_plane",
    "blocked_known_match_not_routable",
    "blocked_unsupported_entry",
    "no_candidate_available",
]


@dataclass(frozen=True)
class PlanningOutcome:
    """One observed entry's dry-run planning outcome."""

    action: PlanAction
    observed_entry_id: str
    agent_id: str
    target_path: str
    classification: str
    plan: RemediationPlan | None = None
    match: CatalogMatch | None = None
    candidate_stage: CandidateStageResult | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "observed_entry_id": self.observed_entry_id,
            "agent_id": self.agent_id,
            "target_path": self.target_path,
            "classification": self.classification,
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "match": self.match.to_dict() if self.match is not None else None,
            "candidate_stage": (
                self.candidate_stage.to_dict() if self.candidate_stage is not None else None
            ),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SelfHealingDryRunResult:
    """Dry-run self-healing result through the PLAN state."""

    ingestion: IngestionResult
    outcomes: list[PlanningOutcome]
    plans: list[RemediationPlan]
    events: list[EventRecord]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ingestion": self.ingestion.to_dict(),
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "plans": [plan.to_dict() for plan in self.plans],
            "events": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True)
class PlanPolicyDecision:
    """Policy decision attached to a dry-run plan."""

    auto_apply_allowed: bool
    approval_required: bool
    approval_reason: str
    reasons: list[str]
    dry_run_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "auto_apply_allowed": self.auto_apply_allowed,
            "approval_required": self.approval_required,
            "approval_reason": self.approval_reason,
            "dry_run_only": self.dry_run_only,
            "reasons": self.reasons,
        }


def plan_self_healing_dry_run(
    connection: sqlite3.Connection,
    observed_entries: list[ObservedEntry],
    *,
    catalog_store: CatalogStore | None = None,
    actor: str = "daemon",
    run_id: str | None = None,
    timestamp: str | None = None,
    include_missing_control_plane: bool = True,
    emit_events: bool = True,
) -> SelfHealingDryRunResult:
    """Run the self-healing state machine through dry-run remediation planning."""
    event_run_id = run_id or _run_id(observed_entries)
    created_at = timestamp or _current_timestamp()
    ingestion = ingest_observed_entries(
        connection,
        observed_entries,
        actor=actor,
        run_id=event_run_id,
        timestamp=timestamp,
        emit_events=emit_events,
    )
    store = catalog_store or CatalogStore(connection)
    outcomes: list[PlanningOutcome] = []
    plans: list[RemediationPlan] = []

    for classified in ingestion.classifications:
        entry = classified.observed_entry
        outcome = _plan_for_classified_entry(
            connection,
            entry,
            classified.classification,
            store,
            created_at=created_at,
        )
        outcomes.append(outcome)
        if outcome.plan is not None:
            plans.append(outcome.plan)

    if include_missing_control_plane:
        for entry_group in _observed_groups_missing_control_plane(ingestion.observed_entries):
            plan = generate_missing_control_plane_plan(
                agent_id=entry_group["agent_id"],
                target_path=entry_group["target_path"],
                expected_preimage_hash=entry_group["expected_preimage_hash"],
                created_at=created_at,
            )
            outcomes.append(
                PlanningOutcome(
                    action="planned_missing_control_plane",
                    observed_entry_id=plan.observed_entry_id,
                    agent_id=plan.agent_id,
                    target_path=plan.target_path,
                    classification="missing_control_plane",
                    plan=plan,
                    reason="mcp_hub control-plane entry is absent",
                )
            )
            plans.append(plan)

    planning_events: list[EventRecord] = []
    if emit_events and plans:
        planning_events.append(
            EventStore(connection).append(
                event_id=_event_id(event_run_id, "remediation.planned"),
                event_type="remediation.planned",
                actor=actor,
                result="success",
                payload={
                    "plan_count": len(plans),
                    "plan_ids": [plan.plan_id for plan in plans],
                    "plan_types": [plan.plan_type for plan in plans],
                    "dry_run_only": True,
                },
                timestamp=timestamp,
            )
        )

    return SelfHealingDryRunResult(
        ingestion=ingestion,
        outcomes=outcomes,
        plans=plans,
        events=[*ingestion.events, *planning_events],
    )


def generate_known_direct_rewrite_plan(
    observed_entry: ObservedEntry,
    catalog_entry: CatalogEntry,
    match: CatalogMatch,
    *,
    created_at: str,
) -> RemediationPlan:
    """Generate a dry-run plan to rewrite a known direct entry through the Hub."""
    if observed_entry.transport == "streamable_http" and observed_entry.url == _hub_url(
        catalog_entry
    ):
        raise PlanningError("observed entry is already routed through the Hub")
    if match.catalog_id != catalog_entry.catalog_id:
        raise PlanningError("catalog match does not identify the supplied catalog entry")
    if match.confidence != "high":
        raise PlanningError("known direct rewrite requires high catalog match confidence")
    if not match.routable:
        raise PlanningError("known direct rewrite requires a routable catalog entry")

    before = _observed_projection(observed_entry)
    after = {
        **before,
        "transport": "streamable_http",
        "command": None,
        "args": [],
        "url": _hub_url(catalog_entry),
    }
    policy = PlanPolicyDecision(
        auto_apply_allowed=False,
        approval_required=True,
        approval_reason="dry_run_review_required",
        reasons=[
            "known_direct_backend_match",
            f"match_confidence:{match.confidence}",
            *[f"match_reason:{reason}" for reason in match.reasons],
        ],
    )
    return _build_plan(
        plan_type="rewrite_known_direct",
        agent_id=observed_entry.agent_id,
        target_path=observed_entry.config_path,
        observed_entry_id=observed_entry.observed_entry_id,
        catalog_id=catalog_entry.catalog_id,
        expected_preimage_hash=observed_entry.entry_hash,
        before=before,
        after=after,
        policy=policy,
        risk={
            "tier": catalog_entry.risk_tier,
            "reasons": [],
            "verification": "parse_config_and_confirm_hub_routed_entry",
            "rollback": "restore_backup_before_apply",
        },
        created_at=created_at,
    )


def _plan_for_classified_entry(
    connection: sqlite3.Connection,
    observed_entry: ObservedEntry,
    classification: str,
    catalog_store: CatalogStore,
    *,
    created_at: str,
) -> PlanningOutcome:
    if classification == "compliant_hub_routed":
        match = match_observed_entry_from_store(observed_entry, catalog_store)
        return PlanningOutcome(
            action="already_compliant",
            observed_entry_id=observed_entry.observed_entry_id,
            agent_id=observed_entry.agent_id,
            target_path=observed_entry.config_path,
            classification=classification,
            match=match,
            reason="entry is already routed through the Hub",
        )
    if classification == "unsupported_entry":
        match = match_observed_entry_from_store(observed_entry, catalog_store)
        if match.catalog_id is not None and match.confidence == "high" and match.routable:
            catalog_entry = catalog_store.show(match.catalog_id)
            if observed_entry.transport == "streamable_http" and observed_entry.url == _hub_url(
                catalog_entry
            ):
                return PlanningOutcome(
                    action="already_compliant",
                    observed_entry_id=observed_entry.observed_entry_id,
                    agent_id=observed_entry.agent_id,
                    target_path=observed_entry.config_path,
                    classification=classification,
                    match=match,
                    reason="entry is already routed through the Hub",
                )
        return PlanningOutcome(
            action="blocked_unsupported_entry",
            observed_entry_id=observed_entry.observed_entry_id,
            agent_id=observed_entry.agent_id,
            target_path=observed_entry.config_path,
            classification=classification,
            reason="parser confidence is incomplete; planning is audit-only",
        )

    match = match_observed_entry_from_store(observed_entry, catalog_store)
    if match.catalog_id is not None and match.confidence == "high":
        if not match.routable:
            return PlanningOutcome(
                action="blocked_known_match_not_routable",
                observed_entry_id=observed_entry.observed_entry_id,
                agent_id=observed_entry.agent_id,
                target_path=observed_entry.config_path,
                classification=classification,
                match=match,
                reason="catalog match is known but not routable",
            )
        catalog_entry = catalog_store.show(match.catalog_id)
        if observed_entry.transport == "streamable_http" and observed_entry.url == _hub_url(
            catalog_entry
        ):
            return PlanningOutcome(
                action="already_compliant",
                observed_entry_id=observed_entry.observed_entry_id,
                agent_id=observed_entry.agent_id,
                target_path=observed_entry.config_path,
                classification=classification,
                match=match,
                reason="entry is already routed through the Hub",
            )
        plan = generate_known_direct_rewrite_plan(
            observed_entry,
            catalog_entry,
            match,
            created_at=created_at,
        )
        return PlanningOutcome(
            action="planned_known_direct_rewrite",
            observed_entry_id=observed_entry.observed_entry_id,
            agent_id=observed_entry.agent_id,
            target_path=observed_entry.config_path,
            classification=classification,
            plan=plan,
            match=match,
            reason="high-confidence catalog match can be routed through the Hub",
        )

    candidate_stage = stage_unknown_candidate(connection, observed_entry, catalog_store)
    if candidate_stage.candidate is None:
        return PlanningOutcome(
            action="no_candidate_available",
            observed_entry_id=observed_entry.observed_entry_id,
            agent_id=observed_entry.agent_id,
            target_path=observed_entry.config_path,
            classification=classification,
            match=match,
            candidate_stage=candidate_stage,
            reason="entry did not produce a catalog candidate",
        )
    if candidate_stage.candidate.classification == "unknown_local_http":
        plan = generate_unsafe_local_http_plan(
            observed_entry,
            candidate_stage.candidate,
            created_at=created_at,
        )
        return PlanningOutcome(
            action="planned_unsafe_local_http",
            observed_entry_id=observed_entry.observed_entry_id,
            agent_id=observed_entry.agent_id,
            target_path=observed_entry.config_path,
            classification=classification,
            plan=plan,
            match=match,
            candidate_stage=candidate_stage,
            reason="unknown loopback HTTP endpoint requires operator review",
        )

    plan = generate_unknown_import_plan(
        observed_entry,
        candidate_stage.candidate,
        created_at=created_at,
    )
    return PlanningOutcome(
        action="planned_unknown_import",
        observed_entry_id=observed_entry.observed_entry_id,
        agent_id=observed_entry.agent_id,
        target_path=observed_entry.config_path,
        classification=classification,
        plan=plan,
        match=match,
        candidate_stage=candidate_stage,
        reason="unknown direct MCP is staged as a candidate",
    )


def generate_unknown_import_plan(
    observed_entry: ObservedEntry,
    candidate: CatalogCandidate,
    *,
    created_at: str,
) -> RemediationPlan:
    """Generate a dry-run plan to stage an unknown non-local candidate for review."""
    _require_candidate_for_observed_entry(observed_entry, candidate)
    if candidate.classification == "unknown_local_http":
        raise PlanningError("unknown local HTTP candidates require unsafe local HTTP plans")
    before = _observed_projection(observed_entry)
    after = {
        "catalog_candidate": candidate.to_dict(),
        "catalog_entry": {
            "catalog_id": _future_catalog_id(candidate),
            "name": candidate.proposed_name,
            "review_state": "pending",
            "lifecycle_state": "disabled",
            "source_candidate_id": candidate.candidate_id,
        },
    }
    policy = PlanPolicyDecision(
        auto_apply_allowed=False,
        approval_required=True,
        approval_reason="unknown_candidate_review_required",
        reasons=[*candidate.reasons, f"classification:{candidate.classification}"],
    )
    return _build_plan(
        plan_type="import_unknown_candidate",
        agent_id=observed_entry.agent_id,
        target_path=observed_entry.config_path,
        observed_entry_id=observed_entry.observed_entry_id,
        catalog_id=_future_catalog_id(candidate),
        expected_preimage_hash=observed_entry.entry_hash,
        before=before,
        after=after,
        policy=policy,
        risk={
            "tier": candidate.risk_tier,
            "reasons": candidate.reasons,
            "verification": "operator_reviews_candidate_before_catalog_import",
            "rollback": "delete_pending_candidate_before_apply",
        },
        created_at=created_at,
    )


def generate_unsafe_local_http_plan(
    observed_entry: ObservedEntry,
    candidate: CatalogCandidate,
    *,
    created_at: str,
) -> RemediationPlan:
    """Generate a dry-run blocker plan for an unknown loopback HTTP MCP endpoint."""
    _require_candidate_for_observed_entry(observed_entry, candidate)
    if candidate.classification != "unknown_local_http":
        raise PlanningError("unsafe local HTTP plan requires an unknown_local_http candidate")
    before = _observed_projection(observed_entry)
    after = {
        "catalog_candidate": candidate.to_dict(),
        "required_action": "review_local_http_endpoint_before_import_or_route",
    }
    policy = PlanPolicyDecision(
        auto_apply_allowed=False,
        approval_required=True,
        approval_reason="unsafe_local_http_candidate",
        reasons=[*candidate.reasons, "loopback_http_endpoint_cannot_be_auto_trusted"],
    )
    return _build_plan(
        plan_type="unsafe_local_http_detected",
        agent_id=observed_entry.agent_id,
        target_path=observed_entry.config_path,
        observed_entry_id=observed_entry.observed_entry_id,
        catalog_id=_future_catalog_id(candidate),
        expected_preimage_hash=observed_entry.entry_hash,
        before=before,
        after=after,
        policy=policy,
        risk={
            "tier": "high",
            "reasons": [*candidate.reasons, "local_endpoint_may_be_unmanaged_process"],
            "verification": "operator_confirms_process_owner_before_any_route",
            "rollback": "no_config_mutation_performed",
        },
        created_at=created_at,
    )


def generate_missing_control_plane_plan(
    *,
    agent_id: str,
    target_path: str,
    expected_preimage_hash: str,
    created_at: str,
) -> RemediationPlan:
    """Generate a dry-run plan for installing the `mcp_hub` control-plane entry."""
    observed_entry_id = _synthetic_observed_id(
        "missing_control_plane",
        agent_id,
        target_path,
        expected_preimage_hash,
    )
    before = {"mcp_hub": None}
    auth_contract = _control_plane_auth_contract(agent_id)
    after = {
        "mcp_hub": {
            "transport": "streamable_http",
            "url": f"{HUB_BASE_URL}/servers/mcp_hub/mcp",
            "role": "control_plane",
            "auth": auth_contract,
            "headers_present": ["Authorization"],
        }
    }
    policy = PlanPolicyDecision(
        auto_apply_allowed=False,
        approval_required=True,
        approval_reason="missing_control_plane_review_required",
        reasons=["mcp_hub_control_plane_missing", "dry_run_review_required"],
    )
    return _build_plan(
        plan_type="install_missing_control_plane",
        agent_id=agent_id,
        target_path=target_path,
        observed_entry_id=observed_entry_id,
        catalog_id=CONTROL_PLANE_CATALOG_ID,
        expected_preimage_hash=expected_preimage_hash,
        before=before,
        after=after,
        policy=policy,
        risk={
            "tier": "normal",
            "reasons": ["control_plane_entry_absent", "control_plane_auth_required"],
            "verification": "parse_config_and_confirm_authenticated_mcp_hub_entry",
            "rollback": "restore_backup_before_apply",
        },
        created_at=created_at,
    )


def unified_json_diff(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    fromfile: str = "before",
    tofile: str = "after",
) -> str:
    """Return a deterministic unified diff for two JSON-compatible objects."""
    before_lines = _canonical_json(before).splitlines()
    after_lines = _canonical_json(after).splitlines()
    return "\n".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
        )
    )


def _build_plan(
    *,
    plan_type: PlanKind,
    agent_id: str,
    target_path: str,
    observed_entry_id: str,
    catalog_id: str,
    expected_preimage_hash: str,
    before: dict[str, Any],
    after: dict[str, Any],
    policy: PlanPolicyDecision,
    risk: dict[str, Any],
    created_at: str,
) -> RemediationPlan:
    payload = {
        "schema_version": 1,
        "plan_id": _plan_id(
            plan_type,
            agent_id,
            target_path,
            observed_entry_id,
            catalog_id,
            expected_preimage_hash,
        ),
        "plan_type": plan_type,
        "status": "pending_approval" if policy.approval_required else "draft",
        "agent_id": agent_id,
        "target_path": target_path,
        "observed_entry_id": observed_entry_id,
        "catalog_id": catalog_id,
        "policy": {
            **policy.to_dict(),
            "target_path": target_path,
            "before": before,
            "after": after,
        },
        "diff": {"format": "unified", "text": unified_json_diff(before, after)},
        "expected_preimage_hash": expected_preimage_hash,
        "rollback_strategy": str(risk["rollback"]),
        "risk": risk,
        "created_at": created_at,
    }
    return RemediationPlan.from_dict(payload)


def _observed_projection(observed_entry: ObservedEntry) -> dict[str, Any]:
    return {
        "target_path": observed_entry.config_path,
        "container_path": observed_entry.container_path,
        "mount_name": observed_entry.mount_name,
        "enabled": observed_entry.enabled,
        "transport": observed_entry.transport,
        "command": observed_entry.command,
        "args": observed_entry.args,
        "url": observed_entry.url,
        "headers_present": observed_entry.headers_present,
        "env_names": observed_entry.env_names,
        "cwd": observed_entry.cwd,
        "tool_filters": observed_entry.tool_filters,
        "approval_policy": observed_entry.approval_policy,
    }


def _hub_url(catalog_entry: CatalogEntry) -> str:
    return f"{HUB_BASE_URL}{catalog_entry.transport.hub_path}"


def _require_candidate_for_observed_entry(
    observed_entry: ObservedEntry, candidate: CatalogCandidate
) -> None:
    if candidate.observed_entry_id != observed_entry.observed_entry_id:
        raise PlanningError("candidate does not belong to the observed entry")


def _future_catalog_id(candidate: CatalogCandidate) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", candidate.proposed_name.lower()).strip("_")
    if not name:
        name = "candidate"
    digest = hashlib.sha256(candidate.candidate_id.encode()).hexdigest()[:12]
    return f"srv_candidate_{name}_{digest}"


def _synthetic_observed_id(*parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]
    return f"obs_{digest}"


def _control_plane_auth_contract(agent_id: str) -> dict[str, Any]:
    if agent_id.startswith("agent_codex_"):
        return {
            "kind": "bearer_token_env_var",
            "env_var": "MCP_MULTIPLEX_CONTROL_TOKEN",
            "token_ref_required": True,
        }
    return {
        "kind": "authorization_header",
        "header": "Authorization",
        "value_source": "agent_token_secret_ref",
        "token_ref_required": True,
    }


def _plan_id(*parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]
    return f"plan_{digest}"


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True)


def _observed_groups_missing_control_plane(
    observed_entries: list[ObservedEntry],
) -> list[dict[str, str]]:
    groups: dict[tuple[str, str], list[ObservedEntry]] = {}
    for entry in observed_entries:
        groups.setdefault((entry.agent_id, entry.config_path), []).append(entry)

    missing: list[dict[str, str]] = []
    for (agent_id, target_path), entries in sorted(groups.items()):
        if any(entry.mount_name == "mcp_hub" for entry in entries):
            continue
        missing.append(
            {
                "agent_id": agent_id,
                "target_path": target_path,
                "expected_preimage_hash": _config_snapshot_hash(entries),
            }
        )
    return missing


def _config_snapshot_hash(observed_entries: list[ObservedEntry]) -> str:
    payload = [
        {
            "observed_entry_id": entry.observed_entry_id,
            "entry_hash": entry.entry_hash,
            "mount_name": entry.mount_name,
            "target_path": entry.config_path,
        }
        for entry in sorted(
            observed_entries,
            key=lambda item: (item.agent_id, item.config_path, item.mount_name, item.entry_hash),
        )
    ]
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    return f"sha256:{digest}"


def _run_id(observed_entries: list[ObservedEntry]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            [
                {
                    "observed_entry_id": entry.observed_entry_id,
                    "entry_hash": entry.entry_hash,
                }
                for entry in sorted(
                    observed_entries,
                    key=lambda item: (item.agent_id, item.config_path, item.observed_entry_id),
                )
            ],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    return f"run_plan_{digest[:24]}"


def _event_id(run_id: str, event_type: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0{event_type}".encode()).hexdigest()
    return f"evt_{digest[:24]}"


def _current_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "CONTROL_PLANE_CATALOG_ID",
    "HUB_BASE_URL",
    "PlanPolicyDecision",
    "PlanningOutcome",
    "PlanningError",
    "SelfHealingDryRunResult",
    "generate_known_direct_rewrite_plan",
    "generate_missing_control_plane_plan",
    "generate_unknown_import_plan",
    "generate_unsafe_local_http_plan",
    "plan_self_healing_dry_run",
    "unified_json_diff",
]
