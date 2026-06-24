from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_multiplex.adapters import ClaudeCodeAdapterError, parse_claude_code_config
from mcp_multiplex.schemas import ObservedEntry

FIXTURE_DIR = Path("tests/fixtures/agents/claude_code")


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
def test_claude_code_fixtures_normalize_to_expected_observed_entries(name: str) -> None:
    parsed = parse_claude_code_config(FIXTURE_DIR / f"{name}.input.json")
    expected = json.loads((FIXTURE_DIR / f"{name}.expected-observed.json").read_text())

    assert parsed.observed_dicts() == expected
    for observed in parsed.observed_dicts():
        assert ObservedEntry.from_dict(observed).to_dict() == observed


def test_claude_code_unsupported_fields_are_preserved() -> None:
    parsed = parse_claude_code_config(FIXTURE_DIR / "unsupported-field.input.json")
    expected = json.loads((FIXTURE_DIR / "unsupported-field.expected-unsupported.json").read_text())

    assert parsed.unsupported_fields == expected
    assert parsed.observed_entries[0].parser_confidence == "partial"


def test_claude_code_headers_are_supported_without_values(tmp_path: Path) -> None:
    config = tmp_path / "claude.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcp_hub": {
                        "type": "http",
                        "url": "http://127.0.0.1:30000/servers/mcp_hub/mcp",
                        "headers": {"Authorization": "Bearer raw-token"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_claude_code_config(config)

    assert parsed.unsupported_fields == {}
    assert parsed.observed_entries[0].headers_present == ["Authorization"]
    assert "raw-token" not in json.dumps(parsed.observed_dicts(), sort_keys=True)


def test_claude_code_headers_helper_marks_authorization_without_values(tmp_path: Path) -> None:
    config = tmp_path / "claude.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcp_hub": {
                        "type": "http",
                        "url": "http://127.0.0.1:30000/servers/mcp_hub/mcp",
                        "headersHelper": "/home/user/.mcp-multiplex/helper.sh",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_claude_code_config(config)

    assert parsed.unsupported_fields == {}
    assert parsed.observed_entries[0].headers_present == ["Authorization"]


def test_claude_code_parser_does_not_implement_rewrite_behavior() -> None:
    parsed = parse_claude_code_config(FIXTURE_DIR / "direct-context7.input.json")

    assert not hasattr(parsed, "rewrite")
    assert parsed.config_path == "tests/fixtures/agents/claude_code/direct-context7.input.json"


def test_claude_code_invalid_json_is_actionable(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"mcpServers": ', encoding="utf-8")

    with pytest.raises(ClaudeCodeAdapterError, match="invalid Claude Code JSON"):
        parse_claude_code_config(config)


def test_claude_code_entry_without_command_or_url_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"mcpServers": {"empty": {"args": []}}}', encoding="utf-8")

    with pytest.raises(ClaudeCodeAdapterError, match="needs command or url"):
        parse_claude_code_config(config)
