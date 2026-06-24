from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.storage import connect, dump_schema, list_migrations, migrate
from mcp_multiplex.storage.migrations import applied_migrations

REQUIRED_TABLES = {
    "schema_migrations",
    "agents",
    "agent_config_paths",
    "catalog_entries",
    "catalog_aliases",
    "catalog_provenance",
    "catalog_candidates",
    "profiles",
    "profile_servers",
    "observed_entries",
    "remediation_plans",
    "approvals",
    "config_backups",
    "runtime_backends",
    "runtime_frontend_sessions",
    "credential_refs",
    "auth_tokens",
    "agent_registration_tokens",
    "events",
}


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_schema
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return {str(row["name"]) for row in rows}


def test_list_migrations_finds_core_migration() -> None:
    migrations = list_migrations()

    assert [migration.version for migration in migrations] == ["0001"]
    assert migrations[0].name == "core_tables"
    assert "CREATE TABLE IF NOT EXISTS agents" in migrations[0].sql
    assert len(migrations[0].checksum) == 64


def test_fresh_database_migrates_from_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)

    applied = migrate(connection)

    assert [record.version for record in applied] == ["0001"]
    assert table_names(connection) >= REQUIRED_TABLES
    records = applied_migrations(connection)
    assert set(records) == {"0001"}
    assert records["0001"].name == "core_tables"


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "multiplex.db")

    first = migrate(connection)
    second = migrate(connection)

    assert [record.version for record in first] == ["0001"]
    assert second == []
    assert set(applied_migrations(connection)) == {"0001"}


def test_isolated_temp_databases_do_not_share_state(tmp_path: Path) -> None:
    first = connect(tmp_path / "first.db")
    second = connect(tmp_path / "second.db")
    migrate(first)
    migrate(second)

    first.execute(
        """
        INSERT INTO agents (agent_id, agent_kind, display_name)
        VALUES ('agent_one', 'codex', 'Codex')
        """
    )
    first.commit()

    first_count = first.execute("SELECT COUNT(*) AS count FROM agents").fetchone()["count"]
    second_count = second.execute("SELECT COUNT(*) AS count FROM agents").fetchone()["count"]
    assert first_count == 1
    assert second_count == 0


def test_core_foreign_keys_are_enforced(tmp_path: Path) -> None:
    connection = connect(tmp_path / "multiplex.db")
    migrate(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO agent_config_paths (config_path_id, agent_id, path, format)
            VALUES ('path_missing', 'missing_agent', '/tmp/config.toml', 'toml')
            """
        )


def test_schema_dump_contains_reviewable_core_tables(tmp_path: Path) -> None:
    connection = connect(tmp_path / "multiplex.db")
    migrate(connection)

    schema = dump_schema(connection)

    assert "CREATE TABLE agents" in schema
    assert "CREATE TABLE catalog_entries" in schema
    assert "CREATE TABLE observed_entries" in schema
    assert "CREATE TABLE remediation_plans" in schema
    assert "CREATE TABLE auth_tokens" in schema
    assert "CREATE TABLE agent_registration_tokens" in schema
    assert "CREATE TABLE schema_migrations" in schema
    assert "CREATE INDEX idx_events_type_timestamp" in schema
