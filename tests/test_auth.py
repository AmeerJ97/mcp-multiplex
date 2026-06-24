from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.adapters import AgentRegistry
from mcp_multiplex.auth import (
    CONTROL_MUTATE,
    CONTROL_READ,
    AuthError,
    AuthTokenStore,
)
from mcp_multiplex.daemon import require_local_auth
from mcp_multiplex.observability import REDACTED_VALUE, EventStore
from mcp_multiplex.storage import connect


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "multiplex.db")


def test_local_auth_token_is_hashed_and_verifies_scope(
    connection: sqlite3.Connection,
) -> None:
    store = AuthTokenStore(connection)
    issued = store.issue_local_token(
        subject_type="operator",
        subject_id="local-user",
        scopes=[CONTROL_READ, CONTROL_MUTATE],
    )

    context = store.verify_local_token(issued.token, required_scope=CONTROL_MUTATE)

    assert context.subject_type == "operator"
    assert context.subject_id == "local-user"
    assert context.scopes == {CONTROL_READ, CONTROL_MUTATE}
    row = connection.execute(
        "SELECT token_hash, token_ref, last_used_at FROM auth_tokens WHERE token_id = ?",
        (issued.token_id,),
    ).fetchone()
    assert row["token_hash"].startswith("sha256:")
    assert row["token_ref"] == issued.token_ref
    assert row["last_used_at"] is not None
    assert issued.token not in json.dumps(dict(row), sort_keys=True)


def test_local_auth_token_denies_missing_scope(
    connection: sqlite3.Connection,
) -> None:
    issued = AuthTokenStore(connection).issue_local_token(
        subject_type="operator",
        scopes=[CONTROL_READ],
    )

    with pytest.raises(AuthError, match="missing required scope"):
        AuthTokenStore(connection).verify_local_token(issued.token, required_scope=CONTROL_MUTATE)


def test_revoked_local_auth_token_is_not_active(
    connection: sqlite3.Connection,
) -> None:
    store = AuthTokenStore(connection)
    issued = store.issue_local_token(subject_type="operator", scopes=[CONTROL_READ])

    store.revoke_local_token(issued.token_id, revoked_at="2026-06-20T00:00:00Z")

    with pytest.raises(AuthError, match="not active"):
        store.verify_local_token(issued.token)


def test_agent_registration_token_exchanges_once_and_updates_agent_ref(
    connection: sqlite3.Connection,
) -> None:
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    store = AuthTokenStore(connection)
    registration = store.issue_agent_registration_token(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        scopes=[CONTROL_READ],
    )

    issued = store.exchange_agent_registration_token(registration.token)

    assert issued.subject_type == "agent"
    assert issued.subject_id == "agent_codex_user_default"
    assert issued.scopes == [CONTROL_READ]
    row = connection.execute(
        """
        SELECT auth_token_ref
        FROM agents
        WHERE agent_id = 'agent_codex_user_default'
        """
    ).fetchone()
    assert row["auth_token_ref"] == issued.token_ref
    consumed = store.show_agent_registration_token(registration.token_id)
    assert consumed.consumed_at is not None
    with pytest.raises(AuthError, match="not active"):
        store.exchange_agent_registration_token(registration.token)


def test_auth_events_redact_raw_tokens(
    connection: sqlite3.Connection,
) -> None:
    issued = AuthTokenStore(connection).issue_local_token(
        subject_type="operator",
        scopes=[CONTROL_READ],
    )

    events = EventStore(connection).query(event_type="auth.token_issued")

    assert len(events) == 1
    assert events[0].payload["token_ref"] == REDACTED_VALUE
    assert issued.token not in json.dumps(events[0].payload, sort_keys=True)
    event_row = connection.execute(
        "SELECT payload_json FROM events WHERE event_id = ?",
        (events[0].event.event_id,),
    ).fetchone()
    assert issued.token not in str(event_row["payload_json"])


def test_daemon_local_auth_requires_bearer_token_and_scope(
    connection: sqlite3.Connection,
) -> None:
    issued = AuthTokenStore(connection).issue_local_token(
        subject_type="operator",
        scopes=[CONTROL_READ],
    )

    context = require_local_auth(
        connection,
        f"Bearer {issued.token}",
        required_scope=CONTROL_READ,
    )

    assert context.token_ref == issued.token_ref
    with pytest.raises(AuthError, match="missing Authorization"):
        require_local_auth(connection, None, required_scope=CONTROL_READ)
    with pytest.raises(AuthError, match="missing required scope"):
        require_local_auth(connection, f"Bearer {issued.token}", required_scope=CONTROL_MUTATE)
