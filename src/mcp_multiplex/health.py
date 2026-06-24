"""Health payload helpers for the daemon and CLI."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

HealthArea = Literal["daemon", "compliance", "runtime", "credentials", "storage"]


class HealthIssue(TypedDict):
    """Operator-visible health issue."""

    area: HealthArea
    code: str
    detail: str
    agent_id: NotRequired[str]
    server: NotRequired[str]


class HealthSummary(TypedDict):
    """Stable health counters from the schema contract."""

    agents: int
    blockers: int
    warnings: int
    notices: int
    active_servers: int
    hot_backends: int
    pending_approvals: int


class HealthPayload(TypedDict):
    """Schema-versioned MCP Multiplex health payload."""

    schema_version: int
    kind: Literal["MCPMultiplexHealth"]
    ok: bool
    summary: HealthSummary
    blockers: list[HealthIssue]
    warnings: list[HealthIssue]
    notices: list[HealthIssue]


def empty_summary() -> HealthSummary:
    """Return a zeroed health summary for the foundation daemon."""
    return {
        "agents": 0,
        "blockers": 0,
        "warnings": 0,
        "notices": 0,
        "active_servers": 0,
        "hot_backends": 0,
        "pending_approvals": 0,
    }


def healthy_payload() -> HealthPayload:
    """Return the TASK-002 empty-daemon health payload."""
    return {
        "schema_version": 1,
        "kind": "MCPMultiplexHealth",
        "ok": True,
        "summary": empty_summary(),
        "blockers": [],
        "warnings": [],
        "notices": [],
    }


def daemon_unavailable_payload(detail: str) -> HealthPayload:
    """Return a schema-compatible blocker when the daemon cannot be reached."""
    blocker: HealthIssue = {
        "area": "daemon",
        "code": "daemon_unavailable",
        "detail": detail,
    }
    summary = empty_summary()
    summary["blockers"] = 1
    return {
        "schema_version": 1,
        "kind": "MCPMultiplexHealth",
        "ok": False,
        "summary": summary,
        "blockers": [blocker],
        "warnings": [],
        "notices": [],
    }


def is_health_payload(value: Any) -> bool:
    """Validate the stable surface required before full schema models exist."""
    if not isinstance(value, dict):
        return False
    if value.get("schema_version") != 1 or value.get("kind") != "MCPMultiplexHealth":
        return False
    if not isinstance(value.get("ok"), bool):
        return False
    summary = value.get("summary")
    if not isinstance(summary, dict):
        return False
    expected_summary_keys = set(empty_summary())
    if set(summary) != expected_summary_keys:
        return False
    if not all(isinstance(summary[key], int) for key in expected_summary_keys):
        return False
    return all(isinstance(value.get(key), list) for key in ("blockers", "warnings", "notices"))
