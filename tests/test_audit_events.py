from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.observability import REDACTED_VALUE, EventStore, redact_secrets
from mcp_multiplex.storage import connect


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "multiplex.db")


def insert_agent(
    connection: sqlite3.Connection,
    agent_id: str = "agent_codex_user_default",
    agent_kind: str = "codex",
) -> None:
    connection.execute(
        """
        INSERT INTO agents (agent_id, agent_kind, display_name)
        VALUES (?, ?, ?)
        """,
        (agent_id, agent_kind, agent_id),
    )
    connection.commit()


def insert_plan(connection: sqlite3.Connection, plan_id: str = "plan_test") -> None:
    connection.execute(
        """
        INSERT INTO remediation_plans (
          plan_id,
          plan_type,
          status,
          policy_json,
          diff_text,
          risk_json
        )
        VALUES (?, 'rewrite_known_direct', 'pending_approval', '{}', '', '{}')
        """,
        (plan_id,),
    )
    connection.commit()


def test_redact_secrets_recursively() -> None:
    redacted = redact_secrets(
        {
            "api_key": "raw-key",
            "nested": {
                "password": "raw-password",
                "safe": "visible",
                "items": [{"auth_token": "raw-token"}, {"name": "context7"}],
            },
        }
    )

    assert redacted == {
        "api_key": REDACTED_VALUE,
        "nested": {
            "password": REDACTED_VALUE,
            "safe": "visible",
            "items": [{"auth_token": REDACTED_VALUE}, {"name": "context7"}],
        },
    }


def test_append_event_redacts_payload_and_links_hash_chain(connection: sqlite3.Connection) -> None:
    store = EventStore(connection)
    insert_agent(connection)
    first = store.append(
        event_id="evt_first",
        event_type="config.observed",
        actor="daemon",
        agent_id="agent_codex_user_default",
        result="success",
        payload={"token": "raw-token", "safe": "visible"},
        timestamp="2026-06-20T00:00:00Z",
    )
    second = store.append(
        event_id="evt_second",
        event_type="drift.detected",
        actor="daemon",
        agent_id="agent_codex_user_default",
        result="success",
        payload={"detail": "direct bypass"},
        timestamp="2026-06-20T00:00:01Z",
    )

    assert first.payload == {"token": REDACTED_VALUE, "safe": "visible"}
    assert first.event.previous_event_hash is None
    assert second.event.previous_event_hash == first.event.event_hash
    assert store.validate_hash_chain() == []
    stored_payload = connection.execute(
        "SELECT payload_json FROM events WHERE event_id = 'evt_first'"
    ).fetchone()["payload_json"]
    assert "raw-token" not in str(stored_payload)
    assert json.loads(str(stored_payload))["token"] == REDACTED_VALUE


def test_hash_chain_validates_append_order_not_timestamp_order(
    connection: sqlite3.Connection,
) -> None:
    store = EventStore(connection)
    insert_agent(connection)
    first = store.append(
        event_id="evt_first",
        event_type="config.observed",
        actor="daemon",
        agent_id="agent_codex_user_default",
        result="success",
        payload={"detail": "first"},
        timestamp="2026-06-20T00:00:01Z",
    )
    second = store.append(
        event_id="evt_second",
        event_type="drift.detected",
        actor="daemon",
        agent_id="agent_codex_user_default",
        result="success",
        payload={"detail": "second"},
        timestamp="2026-06-20T00:00:00Z",
    )

    assert second.event.previous_event_hash == first.event.event_hash
    assert store.latest_event_hash() == second.event.event_hash
    assert store.validate_hash_chain() == []


def test_events_are_append_only_by_primary_key(connection: sqlite3.Connection) -> None:
    store = EventStore(connection)
    store.append(
        event_id="evt_duplicate",
        event_type="config.observed",
        actor="daemon",
        result="success",
        timestamp="2026-06-20T00:00:00Z",
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.append(
            event_id="evt_duplicate",
            event_type="config.observed",
            actor="daemon",
            result="success",
            timestamp="2026-06-20T00:00:01Z",
        )


def test_query_events_by_type_agent_and_plan(connection: sqlite3.Connection) -> None:
    store = EventStore(connection)
    insert_agent(connection)
    insert_agent(connection, "agent_claude_user_default", "claude_code")
    insert_plan(connection, "plan_rewrite")
    store.append(
        event_id="evt_agent",
        event_type="config.observed",
        actor="daemon",
        agent_id="agent_codex_user_default",
        result="success",
        timestamp="2026-06-20T00:00:00Z",
    )
    store.append(
        event_id="evt_plan",
        event_type="remediation.planned",
        actor="daemon",
        agent_id="agent_codex_user_default",
        plan_id="plan_rewrite",
        result="success",
        timestamp="2026-06-20T00:00:01Z",
    )
    store.append(
        event_id="evt_other",
        event_type="runtime.backend_started",
        actor="daemon",
        agent_id="agent_claude_user_default",
        result="success",
        timestamp="2026-06-20T00:00:02Z",
    )

    assert [record.event.event_id for record in store.query(event_type="config.observed")] == [
        "evt_agent"
    ]
    codex_events = [
        record.event.event_id for record in store.query(agent_id="agent_codex_user_default")
    ]
    assert codex_events == [
        "evt_agent",
        "evt_plan",
    ]
    assert [record.event.event_id for record in store.query(plan_id="plan_rewrite")] == ["evt_plan"]


def test_hash_chain_detects_event_content_tampering(connection: sqlite3.Connection) -> None:
    store = EventStore(connection)
    store.append(
        event_id="evt_first",
        event_type="config.observed",
        actor="daemon",
        result="success",
        payload={"detail": "original"},
        timestamp="2026-06-20T00:00:00Z",
    )

    connection.execute(
        "UPDATE events SET payload_json = ? WHERE event_id = 'evt_first'",
        (json.dumps({"detail": "tampered"}),),
    )
    connection.commit()

    findings = store.validate_hash_chain()
    assert len(findings) == 1
    assert findings[0].event_id == "evt_first"
    assert findings[0].detail == "event_hash does not match row content"


def test_hash_chain_detects_previous_hash_tampering(connection: sqlite3.Connection) -> None:
    store = EventStore(connection)
    store.append(
        event_id="evt_first",
        event_type="config.observed",
        actor="daemon",
        result="success",
        timestamp="2026-06-20T00:00:00Z",
    )
    store.append(
        event_id="evt_second",
        event_type="drift.detected",
        actor="daemon",
        result="success",
        timestamp="2026-06-20T00:00:01Z",
    )

    connection.execute(
        "UPDATE events SET previous_event_hash = 'sha256:bad' WHERE event_id = 'evt_second'"
    )
    connection.commit()

    findings = store.validate_hash_chain()
    assert findings[0].event_id == "evt_second"
    assert findings[0].detail == "previous_event_hash does not match prior event"
