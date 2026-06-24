from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_multiplex.adapters import ClineAdapterError, parse_cline_config
from mcp_multiplex.schemas import ObservedEntry

FIXTURE_DIR = Path("tests/fixtures/agents/cline")


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
def test_cline_fixtures_normalize_to_expected_observed_entries(name: str) -> None:
    parsed = parse_cline_config(FIXTURE_DIR / f"{name}.input.json")
    expected = json.loads((FIXTURE_DIR / f"{name}.expected-observed.json").read_text())

    assert parsed.observed_dicts() == expected
    for observed in parsed.observed_dicts():
        assert ObservedEntry.from_dict(observed).to_dict() == observed


def test_cline_unsupported_fields_are_preserved() -> None:
    parsed = parse_cline_config(FIXTURE_DIR / "unsupported-field.input.json")
    expected = json.loads((FIXTURE_DIR / "unsupported-field.expected-unsupported.json").read_text())

    assert parsed.unsupported_fields == expected
    assert parsed.observed_entries[0].parser_confidence == "partial"


def test_cline_parser_does_not_implement_rewrite_behavior() -> None:
    parsed = parse_cline_config(FIXTURE_DIR / "direct-context7.input.json")

    assert not hasattr(parsed, "rewrite")
    assert parsed.config_path == "tests/fixtures/agents/cline/direct-context7.input.json"


def test_cline_native_streamable_http_transport_is_normalized(tmp_path: Path) -> None:
    config = tmp_path / "cline_mcp_settings.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "context7": {
                        "transport": {
                            "type": "streamableHttp",
                            "url": "http://127.0.0.1:30000/servers/context7/mcp",
                        },
                        "autoApprove": ["ping"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_cline_config(config)

    assert parsed.unsupported_fields == {}
    assert parsed.observed_entries[0].transport == "streamable_http"
    assert parsed.observed_entries[0].url == "http://127.0.0.1:30000/servers/context7/mcp"
    assert parsed.observed_entries[0].parser_confidence == "complete"


def test_cline_empty_command_with_url_is_normalized_as_remote(tmp_path: Path) -> None:
    config = tmp_path / "cline_mcp_settings.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "context7": {
                        "command": "",
                        "args": [],
                        "url": "http://127.0.0.1:30000/servers/context7/mcp",
                        "disabled": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_cline_config(config)

    observed = parsed.observed_entries[0]
    assert observed.transport == "streamable_http"
    assert observed.command is None
    assert observed.url == "http://127.0.0.1:30000/servers/context7/mcp"
    assert observed.enabled is False


def test_cline_transport_headers_are_supported_without_values(tmp_path: Path) -> None:
    config = tmp_path / "cline_mcp_settings.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcp_hub": {
                        "transport": {
                            "type": "streamableHttp",
                            "url": "http://127.0.0.1:30000/servers/mcp_hub/mcp",
                            "headers": {"Authorization": "Bearer raw-token"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_cline_config(config)

    assert parsed.unsupported_fields == {}
    assert parsed.observed_entries[0].headers_present == ["Authorization"]
    assert "raw-token" not in json.dumps(parsed.observed_dicts(), sort_keys=True)


def test_cline_multiplex_helper_command_marks_authenticated_hub(tmp_path: Path) -> None:
    config = tmp_path / "cline_mcp_settings.json"
    helper = tmp_path / ".mcp-multiplex" / "cline-mcp-multiplex-remote.sh"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcp_hub": {
                        "command": str(helper),
                        "args": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_cline_config(config)

    assert parsed.unsupported_fields == {}
    observed = parsed.observed_entries[0]
    assert observed.transport == "streamable_http"
    assert observed.url == "http://127.0.0.1:30000/servers/mcp_hub/mcp"
    assert observed.command == str(helper)
    assert observed.headers_present == ["Authorization"]


def test_cline_arbitrary_command_is_not_treated_as_authenticated_hub(tmp_path: Path) -> None:
    config = tmp_path / "cline_mcp_settings.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcp_hub": {
                        "command": "/tmp/not-multiplex-helper.sh",
                        "args": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_cline_config(config)

    observed = parsed.observed_entries[0]
    assert observed.transport == "stdio"
    assert observed.url is None
    assert observed.headers_present == []


def test_cline_invalid_json_is_actionable(tmp_path: Path) -> None:
    config = tmp_path / "cline_mcp_settings.json"
    config.write_text('{"mcpServers": ', encoding="utf-8")

    with pytest.raises(ClineAdapterError, match="invalid Cline JSON"):
        parse_cline_config(config)


def test_cline_entry_without_command_or_url_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "cline_mcp_settings.json"
    config.write_text('{"mcpServers": {"empty": {"args": []}}}', encoding="utf-8")

    with pytest.raises(ClineAdapterError, match="needs command or url"):
        parse_cline_config(config)
