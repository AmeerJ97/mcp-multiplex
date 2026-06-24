from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_multiplex.cli import main as cli_main
from mcp_multiplex.observability import EventStore
from mcp_multiplex.service import render_user_service_unit
from mcp_multiplex.storage import connect


def test_render_user_service_unit_targets_local_daemon(tmp_path: Path) -> None:
    unit = render_user_service_unit(
        daemon_bin="/opt/mcp-multiplex/bin/mcp-multiplex-daemon",
        host="127.0.0.1",
        port=30000,
        db_path=tmp_path / "multiplex.db",
    )

    assert "Description=MCP Multiplex daemon" in unit
    assert (
        "ExecStart=/opt/mcp-multiplex/bin/mcp-multiplex-daemon --host 127.0.0.1 "
        f"--port 30000 --db-path {tmp_path / 'multiplex.db'}"
    ) in unit
    assert "NoNewPrivileges=true" in unit
    assert "WantedBy=default.target" in unit


def test_cli_daemon_install_user_service_dry_run_does_not_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon_bin = _fake_daemon_bin(tmp_path)
    unit_dir = tmp_path / "systemd-user"
    db_path = tmp_path / "state" / "multiplex.db"

    assert (
        cli_main(
            [
                "daemon",
                "install-user-service",
                "--home",
                str(tmp_path),
                "--unit-dir",
                str(unit_dir),
                "--db-path",
                str(db_path),
                "--daemon-bin",
                str(daemon_bin),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexDaemonUserServiceInstall"
    assert payload["mode"] == "dry_run"
    assert payload["result"]["would_change"] is True
    assert payload["result"]["unit_path"] == str(unit_dir / "mcp-multiplex.service")
    assert f"ExecStart={daemon_bin}" in payload["result"]["unit_text"]
    assert not (unit_dir / "mcp-multiplex.service").exists()
    assert not db_path.exists()


def test_cli_daemon_install_user_service_apply_writes_backup_and_audit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon_bin = _fake_daemon_bin(tmp_path)
    unit_dir = tmp_path / "systemd-user"
    unit_dir.mkdir()
    unit_path = unit_dir / "mcp-multiplex.service"
    unit_path.write_text("[Service]\nExecStart=/legacy/mcp-hub\n", encoding="utf-8")
    db_path = tmp_path / "state" / "multiplex.db"

    assert (
        cli_main(
            [
                "daemon",
                "install-user-service",
                "--apply",
                "--home",
                str(tmp_path),
                "--unit-dir",
                str(unit_dir),
                "--db-path",
                str(db_path),
                "--daemon-bin",
                str(daemon_bin),
                "--actor",
                "test_operator",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    result = payload["result"]
    assert payload["mode"] == "apply"
    assert result["would_change"] is True
    assert result["backup"]["path"].endswith(".bak")
    assert Path(result["backup"]["path"]).read_text(encoding="utf-8") == (
        "[Service]\nExecStart=/legacy/mcp-hub\n"
    )
    installed = unit_path.read_text(encoding="utf-8")
    assert f"ExecStart={daemon_bin}" in installed
    assert "--db-path " + str(db_path.resolve()) in installed

    events = EventStore(connect(db_path)).query(event_type="daemon.service.install")
    assert len(events) == 1
    assert events[0].event.actor == "test_operator"
    assert events[0].event.target_path == str(unit_path)
    assert events[0].payload["backup"]["path"] == result["backup"]["path"]


def test_cli_daemon_status_reports_unit_file_without_systemctl(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unit_dir = tmp_path / "systemd-user"
    unit_dir.mkdir()
    unit_path = unit_dir / "mcp-multiplex.service"
    unit_path.write_text("[Unit]\nDescription=MCP Multiplex daemon\n", encoding="utf-8")

    assert (
        cli_main(
            [
                "daemon",
                "status",
                "--unit-dir",
                str(unit_dir),
                "--no-systemctl",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexDaemonUserServiceStatus"
    assert payload["ok"] is True
    assert payload["result"]["unit_exists"] is True
    assert payload["result"]["unit_path"] == str(unit_path)
    assert payload["result"]["unit_hash"].startswith("sha256:")
    assert payload["result"]["systemctl_available"] is False


def test_cli_daemon_status_reports_systemctl_show_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unit_dir = tmp_path / "systemd-user"
    unit_dir.mkdir()
    (unit_dir / "mcp-multiplex.service").write_text(
        "[Unit]\nDescription=MCP Multiplex daemon\n",
        encoding="utf-8",
    )
    systemctl = _fake_systemctl_bin(tmp_path)

    assert (
        cli_main(
            [
                "daemon",
                "status",
                "--unit-dir",
                str(unit_dir),
                "--systemctl-bin",
                str(systemctl),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["result"]["systemctl_available"] is True
    assert payload["result"]["systemctl_ok"] is True
    assert payload["result"]["systemctl"] == {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
    }


def test_cli_daemon_status_returns_nonzero_when_unit_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli_main(
            [
                "daemon",
                "status",
                "--unit-dir",
                str(tmp_path / "systemd-user"),
                "--no-systemctl",
            ]
        )
        == 1
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["result"]["unit_exists"] is False


def test_cli_daemon_install_user_service_rejects_missing_daemon_bin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing-daemon"

    assert (
        cli_main(
            [
                "daemon",
                "install-user-service",
                "--unit-dir",
                str(tmp_path / "systemd-user"),
                "--daemon-bin",
                str(missing),
            ]
        )
        == 2
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "not executable" in payload["error"]["detail"]


def _fake_daemon_bin(tmp_path: Path) -> Path:
    daemon_bin = tmp_path / "mcp-multiplex-daemon"
    daemon_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    daemon_bin.chmod(0o755)
    return daemon_bin


def _fake_systemctl_bin(tmp_path: Path) -> Path:
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "printf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\n"
        "UnitFileState=enabled\\n'\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    return systemctl
