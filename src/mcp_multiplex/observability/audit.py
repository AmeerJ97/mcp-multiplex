"""Append-only audit/event writer with redaction and hash-chain validation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from mcp_multiplex.schemas import AuditEvent
from mcp_multiplex.storage import migrate

REDACTION_LABEL = "secret_values_removed"
REDACTED_VALUE = "[REDACTED]"
SECRET_KEY_PATTERN = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|access[_-]?key|auth[_-]?token|credential)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EventRecord:
    """Stored audit event plus its redacted payload."""

    event: AuditEvent
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        result = self.event.to_dict()
        result["payload"] = self.payload
        return result


@dataclass(frozen=True)
class TamperFinding:
    """Hash-chain validation failure."""

    event_id: str
    detail: str


def redact_secrets(value: Any) -> Any:
    """Recursively redact secret-like values by key name."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_PATTERN.search(key_text):
                redacted[key_text] = REDACTED_VALUE
            else:
                redacted[key_text] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Return stable JSON for hashing and storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def event_hash_payload(
    *,
    event_id: str,
    event_type: str,
    actor: str,
    agent_id: str | None,
    plan_id: str | None,
    target_path: str | None,
    before_hash: str | None,
    after_hash: str | None,
    backup_id: str | None,
    result: str,
    timestamp: str,
    redaction: str,
    payload: dict[str, Any],
    previous_event_hash: str | None,
) -> dict[str, Any]:
    """Build the canonical hash payload for an event row."""
    return {
        "event_id": event_id,
        "event_type": event_type,
        "actor": actor,
        "agent_id": agent_id,
        "plan_id": plan_id,
        "target_path": target_path,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "backup_id": backup_id,
        "result": result,
        "timestamp": timestamp,
        "redaction": redaction,
        "payload": payload,
        "previous_event_hash": previous_event_hash,
    }


def compute_event_hash(hash_payload: dict[str, Any]) -> str:
    """Compute the tamper-evident event hash."""
    digest = hashlib.sha256(canonical_json(hash_payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class EventStore:
    """Append-only event store backed by SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate(connection)

    def append(
        self,
        *,
        event_id: str,
        event_type: str,
        actor: str,
        result: str,
        payload: dict[str, Any] | None = None,
        agent_id: str | None = None,
        plan_id: str | None = None,
        target_path: str | None = None,
        before_hash: str | None = None,
        after_hash: str | None = None,
        backup_id: str | None = None,
        timestamp: str | None = None,
    ) -> EventRecord:
        """Append one redacted event and return the stored record."""
        redacted_payload = redact_secrets(payload or {})
        previous_hash = self.latest_event_hash()
        event_timestamp = timestamp or self._current_timestamp()
        hash_payload = event_hash_payload(
            event_id=event_id,
            event_type=event_type,
            actor=actor,
            agent_id=agent_id,
            plan_id=plan_id,
            target_path=target_path,
            before_hash=before_hash,
            after_hash=after_hash,
            backup_id=backup_id,
            result=result,
            timestamp=event_timestamp,
            redaction=REDACTION_LABEL,
            payload=redacted_payload,
            previous_event_hash=previous_hash,
        )
        event_hash = compute_event_hash(hash_payload)
        event = AuditEvent.from_dict(
            {
                "schema_version": 1,
                "event_id": event_id,
                "event_type": event_type,
                "actor": actor,
                "agent_id": agent_id,
                "plan_id": plan_id,
                "target_path": target_path,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "backup_id": backup_id,
                "result": result,
                "timestamp": event_timestamp,
                "redaction": REDACTION_LABEL,
                "previous_event_hash": previous_hash,
                "event_hash": event_hash,
            }
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO events (
                  event_id,
                  schema_version,
                  event_type,
                  actor,
                  agent_id,
                  plan_id,
                  target_path,
                  before_hash,
                  after_hash,
                  backup_id,
                  result,
                  redaction,
                  payload_json,
                  previous_event_hash,
                  event_hash,
                  timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.schema_version,
                    event.event_type,
                    event.actor,
                    event.agent_id,
                    event.plan_id,
                    event.target_path,
                    event.before_hash,
                    event.after_hash,
                    event.backup_id,
                    event.result,
                    event.redaction,
                    canonical_json(redacted_payload),
                    event.previous_event_hash,
                    event.event_hash,
                    event.timestamp,
                ),
            )
        return EventRecord(event=event, payload=redacted_payload)

    def latest_event_hash(self) -> str | None:
        """Return the latest event hash, if any."""
        row = self.connection.execute(
            """
            SELECT event_hash
            FROM events
            ORDER BY rowid DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return str(row["event_hash"])

    def query(
        self,
        *,
        event_type: str | None = None,
        agent_id: str | None = None,
        plan_id: str | None = None,
    ) -> list[EventRecord]:
        """Query events by type, agent, and/or plan."""
        clauses: list[str] = []
        params: list[str] = []
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if plan_id is not None:
            clauses.append("plan_id = ?")
            params.append(plan_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM events
            {where}
            ORDER BY timestamp ASC, rowid ASC
            """,
            params,
        ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def validate_hash_chain(self) -> list[TamperFinding]:
        """Return hash-chain tamper findings; empty means valid."""
        findings: list[TamperFinding] = []
        previous_hash: str | None = None
        rows = self.connection.execute(
            """
            SELECT *
            FROM events
            ORDER BY rowid ASC
            """
        ).fetchall()
        for row in rows:
            event_id = str(row["event_id"])
            stored_previous = row["previous_event_hash"]
            if stored_previous != previous_hash:
                findings.append(
                    TamperFinding(
                        event_id=event_id,
                        detail="previous_event_hash does not match prior event",
                    )
                )
            payload = json.loads(str(row["payload_json"]))
            expected = compute_event_hash(
                event_hash_payload(
                    event_id=event_id,
                    event_type=str(row["event_type"]),
                    actor=str(row["actor"]),
                    agent_id=row["agent_id"],
                    plan_id=row["plan_id"],
                    target_path=row["target_path"],
                    before_hash=row["before_hash"],
                    after_hash=row["after_hash"],
                    backup_id=row["backup_id"],
                    result=str(row["result"]),
                    timestamp=str(row["timestamp"]),
                    redaction=str(row["redaction"]),
                    payload=payload,
                    previous_event_hash=stored_previous,
                )
            )
            if row["event_hash"] != expected:
                findings.append(
                    TamperFinding(event_id=event_id, detail="event_hash does not match row content")
                )
            previous_hash = str(row["event_hash"])
        return findings

    def _record_from_row(self, row: sqlite3.Row) -> EventRecord:
        payload = json.loads(str(row["payload_json"]))
        event = AuditEvent.from_dict(
            {
                "schema_version": int(row["schema_version"]),
                "event_id": str(row["event_id"]),
                "event_type": str(row["event_type"]),
                "actor": str(row["actor"]),
                "agent_id": row["agent_id"],
                "plan_id": row["plan_id"],
                "target_path": row["target_path"],
                "before_hash": row["before_hash"],
                "after_hash": row["after_hash"],
                "backup_id": row["backup_id"],
                "result": str(row["result"]),
                "timestamp": str(row["timestamp"]),
                "redaction": str(row["redaction"]),
                "previous_event_hash": row["previous_event_hash"],
                "event_hash": str(row["event_hash"]),
            }
        )
        return EventRecord(event=event, payload=payload)

    def _current_timestamp(self) -> str:
        row = self.connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS now"
        ).fetchone()
        return str(row["now"])
