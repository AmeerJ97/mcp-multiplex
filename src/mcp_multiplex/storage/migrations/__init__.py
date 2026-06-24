"""SQLite migration harness."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class Migration:
    """A packaged SQL migration."""

    version: str
    name: str
    sql: str
    checksum: str


@dataclass(frozen=True)
class MigrationRecord:
    """Applied migration metadata."""

    version: str
    name: str
    checksum: str
    applied_at: str


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with storage invariants enabled."""
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def list_migrations() -> list[Migration]:
    """List packaged SQL migrations in execution order."""
    migration_files = sorted(
        (
            resource
            for resource in files(__package__).iterdir()
            if resource.name.endswith(".sql") and resource.name[:4].isdigit()
        ),
        key=lambda resource: resource.name,
    )
    migrations: list[Migration] = []
    for resource in migration_files:
        sql = resource.read_text(encoding="utf-8")
        version, name_with_suffix = resource.name.split("_", 1)
        migrations.append(
            Migration(
                version=version,
                name=name_with_suffix.removesuffix(".sql"),
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    return migrations


def ensure_migration_table(connection: sqlite3.Connection) -> None:
    """Create the migration metadata table."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          checksum TEXT NOT NULL,
          applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def applied_migrations(connection: sqlite3.Connection) -> dict[str, MigrationRecord]:
    """Return applied migrations keyed by version."""
    ensure_migration_table(connection)
    rows = connection.execute(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {
        str(row["version"]): MigrationRecord(
            version=str(row["version"]),
            name=str(row["name"]),
            checksum=str(row["checksum"]),
            applied_at=str(row["applied_at"]),
        )
        for row in rows
    }


def migrate(connection: sqlite3.Connection) -> list[MigrationRecord]:
    """Apply pending migrations and return the applied migration records."""
    ensure_migration_table(connection)
    applied = applied_migrations(connection)
    applied_now: list[MigrationRecord] = []

    for migration in list_migrations():
        existing = applied.get(migration.version)
        if existing is not None:
            if existing.checksum != migration.checksum:
                raise RuntimeError(
                    "Migration checksum mismatch for "
                    f"{migration.version}: database={existing.checksum} "
                    f"package={migration.checksum}"
                )
            continue

        with connection:
            connection.executescript(migration.sql)
            connection.execute(
                """
                INSERT INTO schema_migrations (version, name, checksum)
                VALUES (?, ?, ?)
                """,
                (migration.version, migration.name, migration.checksum),
            )
        record = applied_migrations(connection)[migration.version]
        applied_now.append(record)
        applied[migration.version] = record

    return applied_now


def dump_schema(connection: sqlite3.Connection) -> str:
    """Return deterministic table/index/view SQL for schema review."""
    rows = connection.execute(
        """
        SELECT type, name, sql
        FROM sqlite_schema
        WHERE sql IS NOT NULL
          AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    statements = [str(row["sql"]).strip() for row in rows]
    return ";\n\n".join(statements) + (";\n" if statements else "")
