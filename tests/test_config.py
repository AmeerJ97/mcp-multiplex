from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_multiplex.cli import main as cli_main
from mcp_multiplex.config import (
    ConfigLoadError,
    default_policy_config,
    inspect_config,
    load_policy_config,
    resolve_environment_layout,
)


def test_resolve_environment_layout_uses_deterministic_home(tmp_path: Path) -> None:
    layout = resolve_environment_layout(home=tmp_path, env={})

    assert layout.config_dir == tmp_path / ".config" / "mcp-multiplex"
    assert layout.state_dir == tmp_path / ".local" / "state" / "mcp-multiplex"
    assert layout.cache_dir == tmp_path / ".cache" / "mcp-multiplex"
    assert layout.policy_path == tmp_path / ".config" / "mcp-multiplex" / "policy.toml"


def test_resolve_environment_layout_honors_overrides(tmp_path: Path) -> None:
    env = {
        "MCP_MULTIPLEX_CONFIG_DIR": str(tmp_path / "cfg"),
        "MCP_MULTIPLEX_STATE_DIR": str(tmp_path / "state"),
        "MCP_MULTIPLEX_CACHE_DIR": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "ignored-config"),
        "XDG_STATE_HOME": str(tmp_path / "ignored-state"),
        "XDG_CACHE_HOME": str(tmp_path / "ignored-cache"),
    }

    layout = resolve_environment_layout(home=tmp_path / "home", env=env)

    assert layout.config_dir == tmp_path / "cfg"
    assert layout.state_dir == tmp_path / "state"
    assert layout.cache_dir == tmp_path / "cache"
    assert layout.policy_path == tmp_path / "cfg" / "policy.toml"


def test_missing_policy_loads_default_without_mutating_files(tmp_path: Path) -> None:
    payload = inspect_config(home=tmp_path, env={})

    assert payload["policy"] == default_policy_config()
    assert payload["policy_exists"] is False
    assert payload["policy_source"] is None
    assert list(tmp_path.iterdir()) == []


def test_load_policy_config_reads_valid_toml(tmp_path: Path) -> None:
    config_dir = tmp_path / ".config" / "mcp-multiplex"
    config_dir.mkdir(parents=True)
    policy_path = config_dir / "policy.toml"
    policy_path.write_text(
        "\n".join(
            [
                "schema_version = 1",
                "[profiles.coding_default]",
                'description = "Default coding profile"',
                "[packs.docs]",
                "enabled = true",
                "[workspace_policy]",
                "active_warning_threshold = 10",
            ]
        ),
        encoding="utf-8",
    )

    policy, source = load_policy_config(policy_path)

    assert source == str(policy_path)
    assert policy["schema_version"] == 1
    assert policy["profiles"]["coding_default"]["description"] == "Default coding profile"
    assert policy["packs"]["docs"]["enabled"] is True
    assert policy["workspace_policy"]["active_warning_threshold"] == 10


def test_load_policy_config_rejects_malformed_toml(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text("schema_version = [", encoding="utf-8")

    with pytest.raises(ConfigLoadError) as error:
        load_policy_config(policy_path)

    assert str(policy_path) in str(error.value)
    assert "Invalid value" in error.value.message


def test_load_policy_config_rejects_invalid_shape(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text('profiles = "not-a-table"', encoding="utf-8")

    with pytest.raises(ConfigLoadError) as error:
        load_policy_config(policy_path)

    assert error.value.message == "profiles must be a TOML table"


def test_cli_config_inspect_uses_temp_home_without_mutating(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_main(["config", "inspect", "--home", str(tmp_path)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexConfigInspect"
    assert payload["paths"]["config_dir"] == str(tmp_path / ".config" / "mcp-multiplex")
    assert payload["policy"] == default_policy_config()
    assert list(tmp_path.iterdir()) == []


def test_cli_config_inspect_reports_malformed_policy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir = tmp_path / ".config" / "mcp-multiplex"
    config_dir.mkdir(parents=True)
    policy_path = config_dir / "policy.toml"
    policy_path.write_text("schema_version = 2", encoding="utf-8")

    exit_code = cli_main(["config", "inspect", "--home", str(tmp_path)])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexConfigInspect"
    assert payload["policy_source"] == str(policy_path)
    assert payload["errors"] == [{"path": str(policy_path), "detail": "schema_version must be 1"}]
