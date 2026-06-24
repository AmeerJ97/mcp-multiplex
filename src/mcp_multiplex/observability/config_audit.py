"""Observed-entry ingestion and health classification."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from mcp_multiplex.health import HealthIssue, HealthPayload, empty_summary
from mcp_multiplex.observability.audit import EventRecord, EventStore
from mcp_multiplex.schemas import HealthPayload as HealthPayloadModel
from mcp_multiplex.schemas import ObservedEntry
from mcp_multiplex.storage import migrate

HUB_BASE_URL = "http://127.0.0.1:30000"

EntryClassification = Literal[
    "compliant_hub_routed",
    "active_direct_bypass",
    "disabled_direct_entry",
    "unsupported_entry",
]


@dataclass(frozen=True)
class ClassifiedObservedEntry:
    """Observed entry plus its audit classification."""

    observed_entry: ObservedEntry
    classification: EntryClassification
    severity: Literal["compliant", "blocker", "warning"]
    issue: HealthIssue | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_entry_id": self.observed_entry.observed_entry_id,
            "agent_id": self.observed_entry.agent_id,
            "agent_kind": self.observed_entry.agent_kind,
            "mount_name": self.observed_entry.mount_name,
            "enabled": self.observed_entry.enabled,
            "classification": self.classification,
            "severity": self.severity,
            "issue": dict(self.issue) if self.issue is not None else None,
        }


@dataclass(frozen=True)
class IngestionResult:
    """Result from one observed-entry ingestion pass."""

    observed_entries: list[ObservedEntry]
    classifications: list[ClassifiedObservedEntry]
    health: HealthPayload
    events: list[EventRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_entries": [entry.to_dict() for entry in self.observed_entries],
            "classifications": [item.to_dict() for item in self.classifications],
            "health": self.health,
            "events": [event.to_dict() for event in self.events],
        }


class ObservedEntryStore:
    """SQLite-backed observed-entry store."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate(connection)

    def upsert_many(self, observed_entries: list[ObservedEntry]) -> None:
        """Insert or update observed entries without mutating source configs."""
        with self.connection:
            for entry in observed_entries:
                self.connection.execute(
                    """
                    INSERT INTO observed_entries (
                      observed_entry_id,
                      schema_version,
                      agent_id,
                      agent_kind,
                      config_path,
                      container_path_json,
                      mount_name,
                      enabled,
                      transport,
                      command,
                      args_json,
                      url,
                      headers_present_json,
                      env_names_json,
                      cwd,
                      tool_filters_json,
                      approval_policy,
                      entry_hash,
                      raw_shape,
                      parser_confidence
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(observed_entry_id) DO UPDATE SET
                      schema_version = excluded.schema_version,
                      agent_id = excluded.agent_id,
                      agent_kind = excluded.agent_kind,
                      config_path = excluded.config_path,
                      container_path_json = excluded.container_path_json,
                      mount_name = excluded.mount_name,
                      enabled = excluded.enabled,
                      transport = excluded.transport,
                      command = excluded.command,
                      args_json = excluded.args_json,
                      url = excluded.url,
                      headers_present_json = excluded.headers_present_json,
                      env_names_json = excluded.env_names_json,
                      cwd = excluded.cwd,
                      tool_filters_json = excluded.tool_filters_json,
                      approval_policy = excluded.approval_policy,
                      entry_hash = excluded.entry_hash,
                      raw_shape = excluded.raw_shape,
                      parser_confidence = excluded.parser_confidence,
                      last_seen_at = CURRENT_TIMESTAMP
                    """,
                    _observed_entry_row(entry),
                )

    def list(self, *, agent_id: str | None = None) -> list[ObservedEntry]:
        """List stored observed entries in deterministic order."""
        clauses: list[str] = []
        params: list[str] = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM observed_entries
            {where}
            ORDER BY agent_id, config_path, mount_name, observed_entry_id
            """,
            params,
        ).fetchall()
        return [_observed_entry_from_row(row) for row in rows]


def ingest_observed_entries(
    connection: sqlite3.Connection,
    observed_entries: list[ObservedEntry],
    *,
    actor: str = "daemon",
    run_id: str | None = None,
    timestamp: str | None = None,
    emit_events: bool = True,
) -> IngestionResult:
    """Store observed entries, classify health, and emit audit events."""
    store = ObservedEntryStore(connection)
    store.upsert_many(observed_entries)
    classifications = classify_observed_entries(observed_entries)
    health = health_payload_for_classifications(observed_entries, classifications)
    events: list[EventRecord] = []
    if emit_events:
        event_store = EventStore(connection)
        event_run_id = run_id or _run_id(observed_entries)
        events.append(
            event_store.append(
                event_id=_event_id(event_run_id, "config.observed"),
                event_type="config.observed",
                actor=actor,
                result="success",
                payload={
                    "observed_count": len(observed_entries),
                    "observed_entry_ids": [
                        entry.observed_entry_id
                        for entry in sorted_observed_entries(observed_entries)
                    ],
                },
                timestamp=timestamp,
            )
        )
        if _has_drift(classifications):
            events.append(
                event_store.append(
                    event_id=_event_id(event_run_id, "config.drift_detected"),
                    event_type="config.drift_detected",
                    actor=actor,
                    result="success",
                    payload={
                        "blockers": [
                            item.to_dict() for item in classifications if item.severity == "blocker"
                        ],
                        "warnings": [
                            item.to_dict() for item in classifications if item.severity == "warning"
                        ],
                    },
                    timestamp=timestamp,
                )
            )
    return IngestionResult(
        observed_entries=sorted_observed_entries(observed_entries),
        classifications=classifications,
        health=health,
        events=events,
    )


def classify_observed_entries(
    observed_entries: list[ObservedEntry],
) -> list[ClassifiedObservedEntry]:
    """Classify observed entries into compliance and operator health issues."""
    classifications = [
        classify_observed_entry(entry) for entry in sorted_observed_entries(observed_entries)
    ]
    return classifications


def classify_observed_entry(entry: ObservedEntry) -> ClassifiedObservedEntry:
    """Classify one normalized observed entry."""
    if entry.parser_confidence != "complete":
        issue = HealthIssue(
            area="compliance",
            code="unsupported_observed_entry",
            detail=(
                f"{entry.agent_kind} entry {entry.mount_name} contains unsupported fields "
                "and is audit-only until reviewed."
            ),
            agent_id=entry.agent_id,
            server=entry.mount_name,
        )
        return ClassifiedObservedEntry(
            observed_entry=entry,
            classification="unsupported_entry",
            severity="warning",
            issue=issue,
        )
    if is_hub_routed(entry):
        return ClassifiedObservedEntry(
            observed_entry=entry,
            classification="compliant_hub_routed",
            severity="compliant",
        )
    if not entry.enabled:
        issue = HealthIssue(
            area="compliance",
            code="disabled_direct_entry",
            detail=(
                f"Disabled {entry.agent_kind} entry {entry.mount_name} is not Hub-routed; "
                "it remains visible but does not block."
            ),
            agent_id=entry.agent_id,
            server=entry.mount_name,
        )
        return ClassifiedObservedEntry(
            observed_entry=entry,
            classification="disabled_direct_entry",
            severity="warning",
            issue=issue,
        )
    issue = HealthIssue(
        area="compliance",
        code="active_direct_bypass",
        detail=(
            f"Active {entry.agent_kind} entry {entry.mount_name} bypasses "
            "http://127.0.0.1:30000/servers/<server>/mcp."
        ),
        agent_id=entry.agent_id,
        server=entry.mount_name,
    )
    return ClassifiedObservedEntry(
        observed_entry=entry,
        classification="active_direct_bypass",
        severity="blocker",
        issue=issue,
    )


def health_payload_for_classifications(
    observed_entries: list[ObservedEntry],
    classifications: list[ClassifiedObservedEntry],
) -> HealthPayload:
    """Build the operator health payload for audit classifications."""
    blockers = [item.issue for item in classifications if item.severity == "blocker" and item.issue]
    warnings = [item.issue for item in classifications if item.severity == "warning" and item.issue]
    summary = empty_summary()
    summary["agents"] = len({entry.agent_id for entry in observed_entries})
    summary["active_servers"] = len(
        [
            item
            for item in classifications
            if item.observed_entry.enabled and item.severity == "compliant"
        ]
    )
    summary["blockers"] = len(blockers)
    summary["warnings"] = len(warnings)
    payload: HealthPayload = {
        "schema_version": 1,
        "kind": "MCPMultiplexHealth",
        "ok": not blockers,
        "summary": summary,
        "blockers": blockers,
        "warnings": warnings,
        "notices": [],
    }
    HealthPayloadModel.from_dict(cast(dict[str, Any], payload))
    return payload


def is_hub_routed(entry: ObservedEntry) -> bool:
    """Return whether an entry uses the governed per-server Hub URL."""
    expected_url = f"{HUB_BASE_URL}/servers/{entry.mount_name}/mcp"
    return entry.enabled and entry.transport == "streamable_http" and entry.url == expected_url


def sorted_observed_entries(observed_entries: list[ObservedEntry]) -> list[ObservedEntry]:
    return sorted(
        observed_entries,
        key=lambda entry: (
            entry.agent_id,
            entry.config_path,
            entry.mount_name,
            entry.observed_entry_id,
        ),
    )


def _observed_entry_row(entry: ObservedEntry) -> tuple[Any, ...]:
    return (
        entry.observed_entry_id,
        entry.schema_version,
        entry.agent_id,
        entry.agent_kind,
        entry.config_path,
        json.dumps(entry.container_path, sort_keys=True),
        entry.mount_name,
        int(entry.enabled),
        entry.transport,
        entry.command,
        json.dumps(entry.args, sort_keys=True),
        entry.url,
        json.dumps(entry.headers_present, sort_keys=True),
        json.dumps(entry.env_names, sort_keys=True),
        entry.cwd,
        json.dumps(entry.tool_filters, sort_keys=True),
        entry.approval_policy,
        entry.entry_hash,
        entry.raw_shape,
        entry.parser_confidence,
    )


def _observed_entry_from_row(row: sqlite3.Row) -> ObservedEntry:
    return ObservedEntry.from_dict(
        {
            "schema_version": int(row["schema_version"]),
            "observed_entry_id": str(row["observed_entry_id"]),
            "agent_id": str(row["agent_id"]),
            "agent_kind": str(row["agent_kind"]),
            "config_path": str(row["config_path"]),
            "container_path": json.loads(str(row["container_path_json"])),
            "mount_name": str(row["mount_name"]),
            "enabled": bool(row["enabled"]),
            "transport": str(row["transport"]),
            "command": row["command"],
            "args": json.loads(str(row["args_json"])),
            "url": row["url"],
            "headers_present": json.loads(str(row["headers_present_json"])),
            "env_names": json.loads(str(row["env_names_json"])),
            "cwd": row["cwd"],
            "tool_filters": json.loads(str(row["tool_filters_json"])),
            "approval_policy": row["approval_policy"],
            "entry_hash": str(row["entry_hash"]),
            "raw_shape": str(row["raw_shape"]),
            "parser_confidence": str(row["parser_confidence"]),
        }
    )


def _has_drift(classifications: list[ClassifiedObservedEntry]) -> bool:
    return any(item.severity in {"blocker", "warning"} for item in classifications)


def _run_id(observed_entries: list[ObservedEntry]) -> str:
    payload = [entry.to_dict() for entry in sorted_observed_entries(observed_entries)]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:24]


def _event_id(run_id: str, event_type: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0{event_type}".encode()).hexdigest()
    return f"evt_{digest[:24]}"
