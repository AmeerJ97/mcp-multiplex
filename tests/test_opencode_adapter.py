from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_multiplex.adapters import OpenCodeAdapterError, parse_opencode_config
from mcp_multiplex.schemas import ObservedEntry

FIXTURE_DIR = Path("tests/fixtures/agents/opencode")
INPUT_EXTENSIONS = {
    "env-cwd-args": ".input.jsonc",
}


def input_path(name: str) -> Path:
    return FIXTURE_DIR / f"{name}{INPUT_EXTENSIONS.get(name, '.input.json')}"


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
def test_opencode_fixtures_normalize_to_expected_observed_entries(name: str) -> None:
    parsed = parse_opencode_config(input_path(name))
    expected = json.loads((FIXTURE_DIR / f"{name}.expected-observed.json").read_text())

    assert parsed.observed_dicts() == expected
    for observed in parsed.observed_dicts():
        assert ObservedEntry.from_dict(observed).to_dict() == observed


def test_opencode_unsupported_fields_are_preserved() -> None:
    parsed = parse_opencode_config(FIXTURE_DIR / "unsupported-field.input.json")
    expected = json.loads((FIXTURE_DIR / "unsupported-field.expected-unsupported.json").read_text())

    assert parsed.unsupported_fields == expected
    assert parsed.observed_entries[0].parser_confidence == "partial"


def test_opencode_parser_does_not_implement_rewrite_behavior() -> None:
    parsed = parse_opencode_config(FIXTURE_DIR / "direct-context7.input.json")

    assert not hasattr(parsed, "rewrite")
    assert parsed.config_path == "tests/fixtures/agents/opencode/direct-context7.input.json"


def test_opencode_native_remote_mcp_shape_is_normalized(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {
                    "context7": {
                        "type": "remote",
                        "url": "http://127.0.0.1:30000/servers/context7/mcp",
                        "enabled": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_opencode_config(config)

    assert parsed.unsupported_fields == {}
    assert parsed.observed_entries[0].container_path == ["mcp", "context7"]
    assert parsed.observed_entries[0].enabled is False
    assert parsed.observed_entries[0].transport == "streamable_http"
    assert parsed.observed_entries[0].url == "http://127.0.0.1:30000/servers/context7/mcp"


def test_opencode_headers_are_supported_without_values(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        json.dumps(
            {
                "mcp": {
                    "mcp_hub": {
                        "type": "remote",
                        "url": "http://127.0.0.1:30000/servers/mcp_hub/mcp",
                        "headers": {"Authorization": "Bearer raw-token"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_opencode_config(config)

    assert parsed.unsupported_fields == {}
    assert parsed.observed_entries[0].headers_present == ["Authorization"]
    assert "raw-token" not in json.dumps(parsed.observed_dicts(), sort_keys=True)


def test_opencode_oauth_false_env_header_template_is_supported(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        json.dumps(
            {
                "mcp": {
                    "mcp_hub": {
                        "type": "remote",
                        "url": "http://127.0.0.1:30000/servers/mcp_hub/mcp",
                        "oauth": False,
                        "headers": {"Authorization": "Bearer {env:MCP_MULTIPLEX_CONTROL_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_opencode_config(config)

    assert parsed.unsupported_fields == {}
    assert parsed.observed_entries[0].headers_present == ["Authorization"]


def test_opencode_native_local_command_array_is_normalized(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        json.dumps(
            {
                "mcp": {
                    "context7": {
                        "type": "local",
                        "command": ["python", "/tmp/fake.py"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_opencode_config(config)

    assert parsed.observed_entries[0].container_path == ["mcp", "context7"]
    assert parsed.observed_entries[0].transport == "stdio"
    assert parsed.observed_entries[0].command == "python"
    assert parsed.observed_entries[0].args == ["/tmp/fake.py"]


def test_opencode_invalid_json_is_actionable(tmp_path: Path) -> None:
    config = tmp_path / "opencode.json"
    config.write_text('{"mcpServers": ', encoding="utf-8")

    with pytest.raises(OpenCodeAdapterError, match="invalid OpenCode JSON"):
        parse_opencode_config(config)


def test_opencode_entry_without_command_or_url_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "opencode.json"
    config.write_text('{"mcpServers": {"empty": {"args": []}}}', encoding="utf-8")

    with pytest.raises(OpenCodeAdapterError, match="needs command or url"):
        parse_opencode_config(config)


def test_opencode_jsonc_comment_stripping_preserves_url_like_strings(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        """
        {
          /* Block comment before servers. */
          "mcpServers": {
            "remote": {
              "url": "http://127.0.0.1:30000/servers/remote/mcp" // trailing comment
            }
          }
        }
        """,
        encoding="utf-8",
    )

    parsed = parse_opencode_config(config)

    assert parsed.observed_entries[0].url == "http://127.0.0.1:30000/servers/remote/mcp"
