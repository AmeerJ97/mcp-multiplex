from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_multiplex.adapters import CodexAdapterError, parse_codex_config
from mcp_multiplex.schemas import ObservedEntry

FIXTURE_DIR = Path("tests/fixtures/agents/codex")


@pytest.mark.parametrize(
    "name",
    [
        "direct-context7",
        "hub-routed",
        "disabled-entry",
        "env-cwd-args",
        "unsupported-field",
    ],
)
def test_codex_fixtures_normalize_to_expected_observed_entries(name: str) -> None:
    parsed = parse_codex_config(FIXTURE_DIR / f"{name}.input.toml")
    expected = json.loads((FIXTURE_DIR / f"{name}.expected-observed.json").read_text())

    assert parsed.observed_dicts() == expected
    for observed in parsed.observed_dicts():
        assert ObservedEntry.from_dict(observed).to_dict() == observed


def test_codex_unsupported_fields_are_preserved() -> None:
    parsed = parse_codex_config(FIXTURE_DIR / "unsupported-field.input.toml")
    expected = json.loads((FIXTURE_DIR / "unsupported-field.expected-unsupported.json").read_text())

    assert parsed.unsupported_fields == expected
    assert parsed.observed_entries[0].parser_confidence == "partial"


def test_codex_bearer_token_env_var_is_supported_without_token_value(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.mcp_hub]\n"
        'url = "http://127.0.0.1:30000/servers/mcp_hub/mcp"\n'
        'bearer_token_env_var = "MCP_MULTIPLEX_CONTROL_TOKEN"\n',
        encoding="utf-8",
    )

    parsed = parse_codex_config(config)

    assert parsed.unsupported_fields == {}
    assert parsed.observed_entries[0].headers_present == ["Authorization"]
    assert "MCP_MULTIPLEX_CONTROL_TOKEN" not in json.dumps(parsed.observed_dicts())


def test_codex_parser_does_not_implement_rewrite_behavior() -> None:
    parsed = parse_codex_config(FIXTURE_DIR / "direct-context7.input.toml")

    assert not hasattr(parsed, "rewrite")
    assert parsed.config_path == "tests/fixtures/agents/codex/direct-context7.input.toml"


def test_codex_invalid_toml_is_actionable(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[mcp_servers.context7\n", encoding="utf-8")

    with pytest.raises(CodexAdapterError, match="invalid Codex TOML"):
        parse_codex_config(config)


def test_codex_entry_without_command_or_url_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[mcp_servers.empty]\nargs = []\n", encoding="utf-8")

    with pytest.raises(CodexAdapterError, match="needs command or url"):
        parse_codex_config(config)
