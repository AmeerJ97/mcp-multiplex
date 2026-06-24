from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.adapters import AgentRegistry, parse_codex_config
from mcp_multiplex.observability import EventStore, ObservedEntryStore, ingest_observed_entries
from mcp_multiplex.schemas import ObservedEntry
from mcp_multiplex.storage import connect

FIXTURE_DIR = Path("tests/fixtures/agents/codex")


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "multiplex.db")
    registry = AgentRegistry(connection)
    registry.create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    return connection


def fixture_entry(name: str) -> ObservedEntry:
    return parse_codex_config(FIXTURE_DIR / f"{name}.input.toml").observed_entries[0]


def test_ingestion_stores_observed_entries(connection: sqlite3.Connection) -> None:
    entry = fixture_entry("hub-routed")

    result = ingest_observed_entries(
        connection,
        [entry],
        run_id="run_store",
        timestamp="2026-06-20T00:00:00Z",
    )

    stored = ObservedEntryStore(connection).list()
    assert stored == [entry]
    assert result.observed_entries == [entry]
    row = connection.execute(
        "SELECT container_path_json, args_json, enabled FROM observed_entries"
    ).fetchone()
    assert json.loads(str(row["container_path_json"])) == ["mcp_servers", "context7"]
    assert json.loads(str(row["args_json"])) == []
    assert row["enabled"] == 1


def test_hub_routed_entry_is_compliant(connection: sqlite3.Connection) -> None:
    result = ingest_observed_entries(
        connection,
        [fixture_entry("hub-routed")],
        run_id="run_compliant",
        timestamp="2026-06-20T00:00:00Z",
    )

    assert [item.classification for item in result.classifications] == ["compliant_hub_routed"]
    assert result.health["ok"] is True
    assert result.health["summary"]["blockers"] == 0
    assert result.health["summary"]["active_servers"] == 1
    assert [event.event.event_type for event in result.events] == ["config.observed"]


def test_active_direct_bypass_is_blocker(connection: sqlite3.Connection) -> None:
    result = ingest_observed_entries(
        connection,
        [fixture_entry("direct-context7")],
        run_id="run_direct",
        timestamp="2026-06-20T00:00:00Z",
    )

    assert result.health["ok"] is False
    assert result.health["summary"]["blockers"] == 1
    assert result.health["blockers"][0]["code"] == "active_direct_bypass"
    assert result.health["blockers"][0]["agent_id"] == "agent_codex_user_default"
    assert result.health["blockers"][0]["server"] == "context7"
    assert [item.classification for item in result.classifications] == ["active_direct_bypass"]


def test_disabled_unknown_direct_entry_warns_without_blocking(
    connection: sqlite3.Connection,
) -> None:
    result = ingest_observed_entries(
        connection,
        [fixture_entry("disabled-entry")],
        run_id="run_disabled",
        timestamp="2026-06-20T00:00:00Z",
    )

    assert result.health["ok"] is True
    assert result.health["summary"]["blockers"] == 0
    assert result.health["summary"]["warnings"] == 1
    assert result.health["warnings"][0]["code"] == "disabled_direct_entry"
    assert [item.classification for item in result.classifications] == ["disabled_direct_entry"]


def test_unsupported_partial_entry_warns_and_preserves_visibility(
    connection: sqlite3.Connection,
) -> None:
    result = ingest_observed_entries(
        connection,
        [fixture_entry("unsupported-field")],
        run_id="run_unsupported",
        timestamp="2026-06-20T00:00:00Z",
    )

    assert result.health["ok"] is True
    assert result.health["summary"]["warnings"] == 1
    assert result.health["warnings"][0]["code"] == "unsupported_observed_entry"
    assert [item.classification for item in result.classifications] == ["unsupported_entry"]


def test_config_observed_and_drift_detected_events_are_emitted(
    connection: sqlite3.Connection,
) -> None:
    result = ingest_observed_entries(
        connection,
        [fixture_entry("direct-context7"), fixture_entry("disabled-entry")],
        run_id="run_events",
        timestamp="2026-06-20T00:00:00Z",
    )

    assert [event.event.event_type for event in result.events] == [
        "config.observed",
        "config.drift_detected",
    ]
    observed_event = result.events[0]
    drift_event = result.events[1]
    assert observed_event.payload["observed_count"] == 2
    assert drift_event.payload["blockers"][0]["classification"] == "active_direct_bypass"
    assert drift_event.payload["warnings"][0]["classification"] == "disabled_direct_entry"
    assert EventStore(connection).validate_hash_chain() == []


def test_health_payload_fixture_for_mixed_audit(connection: sqlite3.Connection) -> None:
    result = ingest_observed_entries(
        connection,
        [
            fixture_entry("hub-routed"),
            fixture_entry("direct-context7"),
            fixture_entry("disabled-entry"),
        ],
        run_id="run_health_fixture",
        timestamp="2026-06-20T00:00:00Z",
    )

    assert result.health == {
        "schema_version": 1,
        "kind": "MCPMultiplexHealth",
        "ok": False,
        "summary": {
            "agents": 1,
            "blockers": 1,
            "warnings": 1,
            "notices": 0,
            "active_servers": 1,
            "hot_backends": 0,
            "pending_approvals": 0,
        },
        "blockers": [
            {
                "area": "compliance",
                "code": "active_direct_bypass",
                "detail": (
                    "Active codex entry context7 bypasses "
                    "http://127.0.0.1:30000/servers/<server>/mcp."
                ),
                "agent_id": "agent_codex_user_default",
                "server": "context7",
            }
        ],
        "warnings": [
            {
                "area": "compliance",
                "code": "disabled_direct_entry",
                "detail": (
                    "Disabled codex entry legacy_docs is not Hub-routed; "
                    "it remains visible but does not block."
                ),
                "agent_id": "agent_codex_user_default",
                "server": "legacy_docs",
            }
        ],
        "notices": [],
    }
