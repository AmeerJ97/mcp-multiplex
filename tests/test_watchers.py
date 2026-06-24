from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.adapters import AgentConfigPath, AgentRegistry
from mcp_multiplex.observability import (
    EventStore,
    ObservedEntryStore,
    PollingAuditWatcher,
    WatchedConfigPath,
    run_config_audit,
)
from mcp_multiplex.storage import connect


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "multiplex.db")
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
        config_paths=[
            AgentConfigPath(
                path=str(tmp_path / ".codex" / "config.toml"),
                format="toml",
                precedence=10,
            )
        ],
    )
    return connection


def write_codex_config(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def direct_codex_config(package: str = "@upstash/context7-mcp") -> str:
    return f'''
[mcp_servers.context7]
command = "npx"
args = ["-y", "{package}"]
'''


def hub_codex_config() -> str:
    return """
[mcp_servers.context7]
url = "http://127.0.0.1:30000/servers/context7/mcp"
"""


def watched_codex(path: Path) -> WatchedConfigPath:
    return WatchedConfigPath(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        path=path,
        format="toml",
        precedence=10,
    )


def test_run_config_audit_parses_and_ingests_changed_config(
    tmp_path: Path,
    connection: sqlite3.Connection,
) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    write_codex_config(config_path, direct_codex_config())

    result = run_config_audit(
        connection,
        [watched_codex(config_path)],
        trigger="startup",
        run_id="run_startup",
        timestamp="2026-06-20T00:00:00Z",
    )

    assert result.health["ok"] is False
    assert result.health["blockers"][0]["code"] == "active_direct_bypass"
    stored = ObservedEntryStore(connection).list(agent_id="agent_codex_user_default")
    assert len(stored) == 1
    assert stored[0].mount_name == "context7"
    assert [record.event.event_type for record in EventStore(connection).query()] == [
        "audit.triggered",
        "config.observed",
        "config.drift_detected",
    ]


def test_file_change_triggers_debounced_reaudit(
    tmp_path: Path,
    connection: sqlite3.Connection,
) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    write_codex_config(config_path, hub_codex_config())
    watcher = PollingAuditWatcher(
        connection,
        [watched_codex(config_path)],
        debounce_seconds=1.0,
        periodic_seconds=100.0,
    )

    write_codex_config(config_path, direct_codex_config())
    assert watcher.poll(now=0.1, timestamp="2026-06-20T00:00:00Z") == []
    assert watcher.poll(now=0.5, timestamp="2026-06-20T00:00:00Z") == []
    events = watcher.poll(now=1.2, timestamp="2026-06-20T00:00:01Z")

    assert len(events) == 1
    assert events[0].trigger == "file_change"
    assert events[0].result.health["blockers"][0]["code"] == "active_direct_bypass"


def test_polling_fallback_runs_periodic_full_audit(
    tmp_path: Path,
    connection: sqlite3.Connection,
) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    write_codex_config(config_path, hub_codex_config())
    watcher = PollingAuditWatcher(
        connection,
        [watched_codex(config_path)],
        debounce_seconds=1.0,
        periodic_seconds=5.0,
    )

    assert watcher.poll(now=0.0, timestamp="2026-06-20T00:00:00Z") == []
    events = watcher.poll(now=5.1, timestamp="2026-06-20T00:00:05Z")

    assert len(events) == 1
    assert events[0].trigger == "periodic"
    assert events[0].result.health["ok"] is True
    assert events[0].result.health["summary"]["active_servers"] == 1


def test_rapid_partial_writes_do_not_audit_until_stable(
    tmp_path: Path,
    connection: sqlite3.Connection,
) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    write_codex_config(config_path, hub_codex_config())
    watcher = PollingAuditWatcher(
        connection,
        [watched_codex(config_path)],
        debounce_seconds=1.0,
        periodic_seconds=100.0,
    )

    write_codex_config(config_path, '[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y",')
    assert watcher.poll(now=0.1, timestamp="2026-06-20T00:00:00Z") == []
    write_codex_config(config_path, direct_codex_config("@upstash/context7-mcp"))
    assert watcher.poll(now=0.9, timestamp="2026-06-20T00:00:00Z") == []
    events = watcher.poll(now=2.0, timestamp="2026-06-20T00:00:02Z")

    assert len(events) == 1
    assert events[0].result.health["summary"]["blockers"] == 1
    assert len(ObservedEntryStore(connection).list()) == 1


def test_startup_audit_sets_periodic_schedule(
    tmp_path: Path,
    connection: sqlite3.Connection,
) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    write_codex_config(config_path, hub_codex_config())
    watcher = PollingAuditWatcher(
        connection,
        [watched_codex(config_path)],
        debounce_seconds=1.0,
        periodic_seconds=5.0,
    )

    startup = watcher.run_startup_audit(now=10.0, timestamp="2026-06-20T00:00:00Z")
    assert startup.trigger == "startup"
    assert watcher.poll(now=14.9, timestamp="2026-06-20T00:00:04Z") == []
    periodic = watcher.poll(now=15.0, timestamp="2026-06-20T00:00:05Z")
    assert [event.trigger for event in periodic] == ["periodic"]
