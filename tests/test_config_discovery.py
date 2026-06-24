from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_multiplex.adapters import discover_config_paths
from mcp_multiplex.cli import main as cli_main


def write_file(path: Path, content: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def top_level_entries(path: Path) -> set[str]:
    return {entry.name for entry in path.iterdir()}


def test_discovery_returns_existing_files_only(tmp_path: Path) -> None:
    codex_config = tmp_path / ".codex" / "config.toml"
    gemini_config = tmp_path / ".gemini" / "settings.json"
    write_file(codex_config, "[mcp_servers]\n")
    write_file(gemini_config)

    result = discover_config_paths(home=tmp_path)

    discovered = {(path.agent_kind, path.path, path.format) for path in result.config_paths}
    assert discovered == {
        ("codex", str(codex_config), "toml"),
        ("gemini", str(gemini_config), "json"),
    }
    assert all("missing" in notice.code for notice in result.notices)
    assert str(tmp_path / ".claude.json") in {notice.path for notice in result.notices}


def test_missing_expected_files_are_notices_not_errors(tmp_path: Path) -> None:
    result = discover_config_paths(home=tmp_path, agent_kinds=["codex"])

    assert result.config_paths == []
    assert len(result.notices) == 1
    assert result.notices[0].agent_kind == "codex"
    assert result.notices[0].code == "expected_config_missing"
    assert result.notices[0].path == str(tmp_path / ".codex" / "config.toml")


def test_discovery_does_not_create_or_mutate_files(tmp_path: Path) -> None:
    existing = tmp_path / ".config" / "opencode" / "opencode.json"
    write_file(existing, "{}")
    before_entries = top_level_entries(tmp_path)
    before_content = existing.read_text(encoding="utf-8")

    discover_config_paths(home=tmp_path)

    assert top_level_entries(tmp_path) == before_entries
    assert existing.read_text(encoding="utf-8") == before_content
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".gemini").exists()


def test_discovery_can_filter_agent_kinds(tmp_path: Path) -> None:
    codex_config = tmp_path / ".codex" / "config.toml"
    gemini_config = tmp_path / ".gemini" / "settings.json"
    write_file(codex_config, "[mcp_servers]\n")
    write_file(gemini_config)

    result = discover_config_paths(home=tmp_path, agent_kinds=["gemini"])

    assert [path.path for path in result.config_paths] == [str(gemini_config)]
    assert all(notice.agent_kind == "gemini" for notice in result.notices)


def test_discovery_rejects_unsupported_agent_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported agent_kind"):
        discover_config_paths(home=tmp_path, agent_kinds=["cursor"])


def test_discovered_config_paths_convert_to_registry_hints(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    write_file(config, "[mcp_servers]\n")

    discovered = discover_config_paths(home=tmp_path, agent_kinds=["codex"]).config_paths[0]
    registry_hint = discovered.to_agent_config_path()

    assert registry_hint.path == str(config)
    assert registry_hint.format == "toml"
    assert registry_hint.precedence == 10


def test_cli_config_discover_in_temp_home(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex_config = tmp_path / ".codex" / "config.toml"
    write_file(codex_config, "[mcp_servers]\n")

    exit_code = cli_main(["config", "discover", "--home", str(tmp_path), "--agents", "codex"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexConfigDiscovery"
    assert payload["config_paths"] == [
        {
            "agent_kind": "codex",
            "path": str(codex_config),
            "format": "toml",
            "precedence": 10,
            "is_project_shared": False,
        }
    ]
    assert payload["notices"] == []


def test_cli_config_discover_reports_unsupported_agent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_main(["config", "discover", "--home", str(tmp_path), "--agents", "cursor"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == [{"detail": "unsupported agent_kind: cursor"}]
