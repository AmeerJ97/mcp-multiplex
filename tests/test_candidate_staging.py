from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.adapters import AgentRegistry, parse_codex_config
from mcp_multiplex.catalog import (
    CatalogCandidateStore,
    CatalogStore,
    candidate_from_observed_entry,
    is_local_http_url,
    stage_unknown_candidate,
)
from mcp_multiplex.observability import ingest_observed_entries
from mcp_multiplex.schemas import CatalogEntry, ObservedEntry
from mcp_multiplex.storage import connect
from tests.test_schema_models import catalog_entry_payload, observed_entry_payload

FIXTURE_DIR = Path("tests/fixtures/agents/codex")


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "multiplex.db")
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    return connection


def catalog_entry() -> CatalogEntry:
    return CatalogEntry.from_dict(catalog_entry_payload())


def unknown_stdio_entry(*, enabled: bool = True) -> ObservedEntry:
    payload = observed_entry_payload()
    payload.update(
        {
            "observed_entry_id": "obs_unknown_stdio",
            "mount_name": "new-server",
            "enabled": enabled,
            "command": "uvx",
            "args": ["some-mcp-server"],
            "entry_hash": "sha256:unknownstdio",
        }
    )
    return ObservedEntry.from_dict(payload)


def unknown_local_http_entry(*, enabled: bool = True) -> ObservedEntry:
    payload = observed_entry_payload()
    payload.update(
        {
            "observed_entry_id": "obs_unknown_local_http",
            "mount_name": "local-http",
            "enabled": enabled,
            "transport": "streamable_http",
            "command": None,
            "args": [],
            "url": "http://127.0.0.1:4567/mcp",
            "entry_hash": "sha256:unknownlocalhttp",
        }
    )
    return ObservedEntry.from_dict(payload)


def test_unknown_stdio_creates_pending_approval_required_candidate(
    connection: sqlite3.Connection,
) -> None:
    entry = unknown_stdio_entry(enabled=False)

    result = stage_unknown_candidate(connection, entry)

    assert result.status == "created"
    assert result.match.confidence == "none"
    assert result.candidate is not None
    assert result.candidate.classification == "unknown_stdio"
    assert result.candidate.review_state == "pending"
    assert result.candidate.approval_required is True
    assert result.candidate.backend_shape == {
        "type": "stdio",
        "command": "uvx",
        "args": ["some-mcp-server"],
        "cwd": None,
        "env_names": [],
    }
    assert result.candidate.reasons == [
        "not_in_catalog",
        "unknown_package",
        "disabled_observed_entry",
    ]
    assert CatalogCandidateStore(connection).list() == [result.candidate]


def test_unknown_local_http_creates_candidate_without_auto_route_and_remains_blocker(
    connection: sqlite3.Connection,
) -> None:
    entry = unknown_local_http_entry()

    result = stage_unknown_candidate(connection, entry)
    audit = ingest_observed_entries(
        connection,
        [entry],
        run_id="run_unknown_local_http",
        timestamp="2026-06-20T00:00:00Z",
        emit_events=False,
    )

    assert result.status == "created"
    assert result.candidate is not None
    assert result.candidate.classification == "unknown_local_http"
    assert result.candidate.approval_required is True
    assert result.candidate.review_state == "pending"
    assert result.candidate.reasons == [
        "not_in_catalog",
        "unsafe_local_http_endpoint",
        "active_direct_bypass",
    ]
    assert result.match.auto_apply_allowed is False
    assert audit.health["ok"] is False
    assert audit.health["blockers"][0]["code"] == "active_direct_bypass"


def test_known_direct_entry_does_not_create_candidate(connection: sqlite3.Connection) -> None:
    store = CatalogStore(connection)
    store.upsert(catalog_entry())
    entry = parse_codex_config(FIXTURE_DIR / "direct-context7.input.toml").observed_entries[0]

    result = stage_unknown_candidate(connection, entry, catalog_store=store)

    assert result.status == "matched_known"
    assert result.candidate is None
    assert result.match.catalog_id == "srv_context7"
    assert CatalogCandidateStore(connection).list() == []


def test_candidate_staging_is_idempotent(connection: sqlite3.Connection) -> None:
    entry = unknown_stdio_entry()

    first = stage_unknown_candidate(connection, entry)
    second = stage_unknown_candidate(connection, entry)

    assert first.status == "created"
    assert second.status == "existing"
    assert first.candidate == second.candidate
    assert len(CatalogCandidateStore(connection).list()) == 1


def test_local_http_detection_is_loopback_only() -> None:
    assert is_local_http_url("http://127.0.0.1:4567/mcp") is True
    assert is_local_http_url("http://localhost:4567/mcp") is True
    assert is_local_http_url("https://example.com/mcp") is False


def test_candidate_from_observed_remote_http_is_low_confidence_remote_candidate() -> None:
    payload = observed_entry_payload()
    payload.update(
        {
            "observed_entry_id": "obs_remote_http",
            "mount_name": "remote-http",
            "transport": "streamable_http",
            "command": None,
            "args": [],
            "url": "https://example.com/mcp",
            "entry_hash": "sha256:remotehttp",
        }
    )
    candidate = candidate_from_observed_entry(ObservedEntry.from_dict(payload))

    assert candidate is not None
    assert candidate.classification == "unknown_remote_http"
    assert candidate.confidence == "low"
    assert candidate.approval_required is True
