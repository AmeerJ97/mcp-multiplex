"""Local user-service installation helpers for the daemon."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp_multiplex.apply import sha256_bytes
from mcp_multiplex.config import resolve_environment_layout
from mcp_multiplex.daemon import DEFAULT_HOST, DEFAULT_PORT
from mcp_multiplex.observability import EventStore
from mcp_multiplex.storage import migrate

UNIT_NAME = "mcp-multiplex.service"
DEFAULT_DAEMON_BIN = "mcp-multiplex-daemon"


class UserServiceInstallError(ValueError):
    """Raised when a daemon user service cannot be planned or installed safely."""


@dataclass(frozen=True)
class UserServiceBackup:
    """Exact-byte backup of a pre-existing systemd user unit."""

    path: str
    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}


@dataclass(frozen=True)
class UserServiceInstallPreview:
    """Dry-run or applied user-service installation summary."""

    unit_name: str
    unit_path: str
    daemon_bin: str
    host: str
    port: int
    db_path: str
    already_installed: bool
    would_change: bool
    before_hash: str
    after_hash: str
    unit_text: str
    backup: UserServiceBackup | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_name": self.unit_name,
            "unit_path": self.unit_path,
            "daemon_bin": self.daemon_bin,
            "host": self.host,
            "port": self.port,
            "db_path": self.db_path,
            "already_installed": self.already_installed,
            "would_change": self.would_change,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "backup": self.backup.to_dict() if self.backup is not None else None,
            "unit_text": self.unit_text,
            "operator_action": (
                "Run `systemctl --user daemon-reload && systemctl --user enable --now "
                "mcp-multiplex.service` after reviewing the installed unit."
            ),
        }


@dataclass(frozen=True)
class UserServiceStatus:
    """Observed daemon user-service status without mutation."""

    unit_name: str
    unit_path: str
    unit_exists: bool
    unit_hash: str | None
    systemctl_available: bool
    systemctl_ok: bool
    systemctl: dict[str, str]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_name": self.unit_name,
            "unit_path": self.unit_path,
            "unit_exists": self.unit_exists,
            "unit_hash": self.unit_hash,
            "systemctl_available": self.systemctl_available,
            "systemctl_ok": self.systemctl_ok,
            "systemctl": self.systemctl,
            "error": self.error,
        }


def plan_user_service_install(
    *,
    home: Path | None = None,
    unit_dir: Path | None = None,
    db_path: Path | None = None,
    daemon_bin: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> UserServiceInstallPreview:
    """Return a non-mutating preview for the daemon systemd user unit."""
    resolved_home = (home or Path.home()).expanduser()
    resolved_unit_dir = _resolve_unit_dir(home=resolved_home, unit_dir=unit_dir)
    resolved_db_path = _resolve_db_path(home=resolved_home, db_path=db_path)
    resolved_daemon_bin = _resolve_daemon_bin(daemon_bin)
    unit_path = resolved_unit_dir / UNIT_NAME
    before_bytes = unit_path.read_bytes() if unit_path.exists() else b""
    unit_text = render_user_service_unit(
        daemon_bin=resolved_daemon_bin,
        host=host,
        port=port,
        db_path=resolved_db_path,
    )
    after_bytes = unit_text.encode("utf-8")
    before_hash = sha256_bytes(before_bytes)
    after_hash = sha256_bytes(after_bytes)
    return UserServiceInstallPreview(
        unit_name=UNIT_NAME,
        unit_path=str(unit_path),
        daemon_bin=resolved_daemon_bin,
        host=host,
        port=port,
        db_path=str(resolved_db_path),
        already_installed=unit_path.exists() and before_hash == after_hash,
        would_change=before_hash != after_hash,
        before_hash=before_hash,
        after_hash=after_hash,
        unit_text=unit_text,
    )


def user_service_status(
    *,
    home: Path | None = None,
    unit_dir: Path | None = None,
    systemctl_bin: str = "systemctl",
    include_systemctl: bool = True,
) -> UserServiceStatus:
    """Return observed status for the daemon user service without side effects."""
    resolved_home = (home or Path.home()).expanduser()
    resolved_unit_dir = _resolve_unit_dir(home=resolved_home, unit_dir=unit_dir)
    unit_path = resolved_unit_dir / UNIT_NAME
    unit_exists = unit_path.exists()
    unit_hash = sha256_bytes(unit_path.read_bytes()) if unit_exists else None
    if not include_systemctl:
        return UserServiceStatus(
            unit_name=UNIT_NAME,
            unit_path=str(unit_path),
            unit_exists=unit_exists,
            unit_hash=unit_hash,
            systemctl_available=False,
            systemctl_ok=False,
            systemctl={},
        )
    return _systemctl_status(
        unit_path=unit_path,
        unit_exists=unit_exists,
        unit_hash=unit_hash,
        systemctl_bin=systemctl_bin,
    )


def install_user_service(
    connection: sqlite3.Connection,
    *,
    home: Path | None = None,
    unit_dir: Path | None = None,
    db_path: Path | None = None,
    daemon_bin: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    actor: str = "local_operator",
    timestamp: str | None = None,
) -> UserServiceInstallPreview:
    """Install the daemon systemd user unit with backup and audit evidence."""
    migrate(connection)
    preview = plan_user_service_install(
        home=home,
        unit_dir=unit_dir,
        db_path=db_path,
        daemon_bin=daemon_bin,
        host=host,
        port=port,
    )
    unit_path = Path(preview.unit_path)
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_existing_unit(unit_path, timestamp=timestamp) if unit_path.exists() else None
    if preview.would_change:
        _atomic_write_text(unit_path, preview.unit_text)
    _verify_installed_unit(unit_path, preview.unit_text)
    EventStore(connection).append(
        event_id=_event_id("daemon.service.install", preview.unit_path, timestamp),
        event_type="daemon.service.install",
        actor=actor,
        result="success",
        target_path=preview.unit_path,
        before_hash=preview.before_hash,
        after_hash=preview.after_hash,
        payload={
            "unit_name": preview.unit_name,
            "unit_path": preview.unit_path,
            "daemon_bin": preview.daemon_bin,
            "host": preview.host,
            "port": preview.port,
            "db_path": preview.db_path,
            "would_change": preview.would_change,
            "backup": backup.to_dict() if backup is not None else None,
        },
        timestamp=timestamp,
    )
    return UserServiceInstallPreview(
        unit_name=preview.unit_name,
        unit_path=preview.unit_path,
        daemon_bin=preview.daemon_bin,
        host=preview.host,
        port=preview.port,
        db_path=preview.db_path,
        already_installed=not preview.would_change,
        would_change=preview.would_change,
        before_hash=preview.before_hash,
        after_hash=preview.after_hash,
        unit_text=preview.unit_text,
        backup=backup,
    )


def render_user_service_unit(
    *,
    daemon_bin: str,
    host: str,
    port: int,
    db_path: Path,
) -> str:
    """Render the governed daemon's systemd user unit."""
    return (
        "[Unit]\n"
        "Description=MCP Multiplex daemon\n"
        "Documentation=https://github.com/local/mcp-multiplex\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={_systemd_escape(daemon_bin)} --host {_systemd_escape(host)} "
        f"--port {port} --db-path {_systemd_escape(str(db_path))}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "NoNewPrivileges=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _resolve_unit_dir(*, home: Path, unit_dir: Path | None) -> Path:
    if unit_dir is not None:
        return unit_dir.expanduser().resolve()
    return (home / ".config" / "systemd" / "user").resolve()


def _resolve_db_path(*, home: Path, db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path.expanduser().resolve()
    return (resolve_environment_layout(home=home).state_dir / "multiplex.db").resolve()


def _resolve_daemon_bin(daemon_bin: str | None) -> str:
    candidate = daemon_bin or DEFAULT_DAEMON_BIN
    expanded = Path(candidate).expanduser()
    if expanded.is_absolute() or "/" in candidate:
        resolved = expanded.resolve()
        _require_executable(resolved)
        return str(resolved)
    found = shutil.which(candidate)
    if found is None:
        raise UserServiceInstallError(f"daemon binary not found on PATH: {candidate}")
    resolved = Path(found).resolve()
    _require_executable(resolved)
    return str(resolved)


def _require_executable(path: Path) -> None:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise UserServiceInstallError(f"daemon binary is not executable: {path}")


def _systemctl_status(
    *,
    unit_path: Path,
    unit_exists: bool,
    unit_hash: str | None,
    systemctl_bin: str,
) -> UserServiceStatus:
    systemctl_path = shutil.which(systemctl_bin)
    if systemctl_path is None:
        return UserServiceStatus(
            unit_name=UNIT_NAME,
            unit_path=str(unit_path),
            unit_exists=unit_exists,
            unit_hash=unit_hash,
            systemctl_available=False,
            systemctl_ok=False,
            systemctl={},
            error=f"systemctl not found on PATH: {systemctl_bin}",
        )
    try:
        completed = subprocess.run(
            [
                systemctl_path,
                "--user",
                "show",
                UNIT_NAME,
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=UnitFileState",
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return UserServiceStatus(
            unit_name=UNIT_NAME,
            unit_path=str(unit_path),
            unit_exists=unit_exists,
            unit_hash=unit_hash,
            systemctl_available=True,
            systemctl_ok=False,
            systemctl={},
            error=str(error),
        )
    systemctl_payload = _parse_systemctl_show(completed.stdout)
    return UserServiceStatus(
        unit_name=UNIT_NAME,
        unit_path=str(unit_path),
        unit_exists=unit_exists,
        unit_hash=unit_hash,
        systemctl_available=True,
        systemctl_ok=completed.returncode == 0,
        systemctl=systemctl_payload,
        error=completed.stderr.strip() or None if completed.returncode != 0 else None,
    )


def _parse_systemctl_show(output: str) -> dict[str, str]:
    payload: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        payload[key] = value
    return payload


def _backup_existing_unit(unit_path: Path, *, timestamp: str | None) -> UserServiceBackup:
    content = unit_path.read_bytes()
    stamp = _backup_timestamp(timestamp)
    backup_dir = unit_path.parent / ".mcp-multiplex-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{unit_path.name}.{stamp}.bak"
    backup_path.write_bytes(content)
    return UserServiceBackup(
        path=str(backup_path),
        sha256=sha256_bytes(content),
        bytes=len(content),
    )


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _verify_installed_unit(unit_path: Path, expected_text: str) -> None:
    actual = unit_path.read_text(encoding="utf-8")
    if actual != expected_text:
        raise UserServiceInstallError("post-install verification failed for daemon user service")


def _systemd_escape(value: str) -> str:
    if value and all(character not in value for character in " \t\n\"'\\"):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _backup_timestamp(timestamp: str | None) -> str:
    value = timestamp or datetime.now(UTC).isoformat()
    return (
        value.replace("+00:00", "Z")
        .replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("Z", "Z")
    )


def _event_id(event_type: str, target_path: str, timestamp: str | None) -> str:
    payload = {
        "event_type": event_type,
        "target_path": target_path,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
    }
    digest = hashlib.sha256(repr(sorted(payload.items())).encode("utf-8")).hexdigest()[:24]
    return f"evt_{digest}"
