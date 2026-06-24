from __future__ import annotations

import json
from pathlib import Path

from mcp_multiplex.adapters import (
    AgentRegistry,
    parse_claude_code_config,
    parse_cline_config,
    parse_codex_config,
    parse_gemini_config,
    parse_opencode_config,
)
from mcp_multiplex.apply import ConfigBackupStore
from mcp_multiplex.auth import CONTROL_READ, AuthTokenStore
from mcp_multiplex.install import (
    CLAUDE_CODE_AGENT_ID,
    CLAUDE_CODE_CONTROL_HELPER,
    CLINE_AGENT_ID,
    CLINE_CONTROL_HELPER,
    CODEX_AGENT_ID,
    CODEX_CONTROL_TOKEN_ENV_VAR,
    GEMINI_AGENT_ID,
    MCP_HUB_URL,
    OPENCODE_AGENT_ID,
    ControlPlaneInstallError,
    control_plane_auth_capabilities,
    control_plane_auth_capability,
    install_claude_code_control_plane,
    install_cline_control_plane,
    install_codex_control_plane,
    install_gemini_control_plane,
    install_opencode_control_plane,
    plan_claude_code_control_plane_install,
    plan_cline_control_plane_install,
    plan_codex_control_plane_install,
    plan_gemini_control_plane_install,
    plan_opencode_control_plane_install,
)
from mcp_multiplex.storage import connect


def test_codex_control_plane_install_dry_run_does_not_mutate(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    before = '[mcp_servers.context7]\ncommand = "npx"\n'
    config_path.write_text(before, encoding="utf-8")

    preview = plan_codex_control_plane_install(config_path=config_path)

    assert preview.agent_id == CODEX_AGENT_ID
    assert preview.env_var == CODEX_CONTROL_TOKEN_ENV_VAR
    assert preview.url == MCP_HUB_URL
    assert preview.would_change is True
    assert preview.backup is None
    assert config_path.read_text(encoding="utf-8") == before


def test_codex_control_plane_install_writes_env_var_auth_and_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "multiplex.db"
    config_path = tmp_path / "config.toml"
    config_path.write_text('[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8")
    connection = connect(db_path)

    result = install_codex_control_plane(connection, config_path=config_path)

    text = config_path.read_text(encoding="utf-8")
    assert "[mcp_servers.mcp_hub]" in text
    assert f'url = "{MCP_HUB_URL}"' in text
    assert f'bearer_token_env_var = "{CODEX_CONTROL_TOKEN_ENV_VAR}"' in text
    assert result.backup is not None
    assert ConfigBackupStore(connection).show(result.backup.backup_id).bytes > 0
    parsed = parse_codex_config(config_path)
    hub = next(entry for entry in parsed.observed_entries if entry.mount_name == "mcp_hub")
    assert hub.headers_present == ["Authorization"]
    assert result.token is not None
    agent = AgentRegistry(connection).show(CODEX_AGENT_ID)
    assert agent.auth_token_ref == result.token.token_ref
    assert agent.certification_level == "certified"
    assert agent.config_paths[0].path == str(config_path.resolve())
    assert (
        AuthTokenStore(connection)
        .verify_local_token(
            result.token.token,
            required_scope=CONTROL_READ,
        )
        .subject_id
        == CODEX_AGENT_ID
    )
    assert result.token.token not in text


def test_codex_control_plane_install_payload_redacts_token_by_default(tmp_path: Path) -> None:
    connection = connect(tmp_path / "multiplex.db")
    result = install_codex_control_plane(connection, config_path=tmp_path / "config.toml")
    assert result.token is not None

    redacted = result.to_dict()
    emitted = result.to_dict(include_token=True)

    assert redacted["token"]["token"] == "[REDACTED]"
    assert emitted["token"]["token"] == result.token.token
    assert result.token.token not in json.dumps(redacted, sort_keys=True)


def test_claude_code_control_plane_install_dry_run_does_not_mutate(tmp_path: Path) -> None:
    config_path = tmp_path / ".claude.json"
    helper_path = tmp_path / ".mcp-multiplex" / CLAUDE_CODE_CONTROL_HELPER
    before = '{"mcpServers":{"context7":{"command":"npx"}}}\n'
    config_path.write_text(before, encoding="utf-8")

    preview = plan_claude_code_control_plane_install(
        config_path=config_path,
        helper_path=helper_path,
    )

    assert preview.agent_id == CLAUDE_CODE_AGENT_ID
    assert preview.env_var == CODEX_CONTROL_TOKEN_ENV_VAR
    assert preview.url == MCP_HUB_URL
    assert preview.helper_path == str(helper_path.resolve())
    assert preview.would_change is True
    assert preview.backup is None
    assert preview.helper_backup is None
    assert config_path.read_text(encoding="utf-8") == before
    assert not helper_path.exists()


def test_claude_code_control_plane_install_writes_headers_helper_and_backup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "multiplex.db"
    config_path = tmp_path / ".claude.json"
    helper_path = tmp_path / ".mcp-multiplex" / CLAUDE_CODE_CONTROL_HELPER
    config_path.write_text('{"mcpServers":{"context7":{"command":"npx"}}}\n', encoding="utf-8")
    connection = connect(db_path)

    result = install_claude_code_control_plane(
        connection,
        config_path=config_path,
        helper_path=helper_path,
    )

    text = config_path.read_text(encoding="utf-8")
    parsed_json = json.loads(text)
    hub = parsed_json["mcpServers"]["mcp_hub"]
    assert hub == {
        "type": "http",
        "url": MCP_HUB_URL,
        "headersHelper": result.helper_path,
    }
    assert result.helper_path is not None
    helper_path = Path(result.helper_path)
    assert helper_path.exists()
    helper_text = helper_path.read_text(encoding="utf-8")
    assert CODEX_CONTROL_TOKEN_ENV_VAR in helper_text
    assert result.backup is not None
    assert result.helper_backup is not None
    assert ConfigBackupStore(connection).show(result.backup.backup_id).bytes > 0
    assert ConfigBackupStore(connection).show(result.helper_backup.backup_id).bytes == 0
    parsed = parse_claude_code_config(config_path)
    observed_hub = next(entry for entry in parsed.observed_entries if entry.mount_name == "mcp_hub")
    assert observed_hub.headers_present == ["Authorization"]
    assert result.token is not None
    agent = AgentRegistry(connection).show(CLAUDE_CODE_AGENT_ID)
    assert agent.auth_token_ref == result.token.token_ref
    assert agent.certification_level == "certified"
    assert agent.config_paths[0].path == str(config_path.resolve())
    assert (
        AuthTokenStore(connection)
        .verify_local_token(
            result.token.token,
            required_scope=CONTROL_READ,
        )
        .subject_id
        == CLAUDE_CODE_AGENT_ID
    )
    assert result.token.token not in text
    assert result.token.token not in helper_text


def test_cline_control_plane_install_dry_run_does_not_mutate(tmp_path: Path) -> None:
    config_path = tmp_path / "cline_mcp_settings.json"
    helper_path = tmp_path / ".mcp-multiplex" / CLINE_CONTROL_HELPER
    before = '{"mcpServers":{"context7":{"command":"npx"}}}\n'
    config_path.write_text(before, encoding="utf-8")

    preview = plan_cline_control_plane_install(
        config_path=config_path,
        helper_path=helper_path,
    )

    assert preview.agent_id == CLINE_AGENT_ID
    assert preview.env_var == CODEX_CONTROL_TOKEN_ENV_VAR
    assert preview.url == MCP_HUB_URL
    assert preview.helper_path == str(helper_path.resolve())
    assert preview.would_change is True
    assert preview.backup is None
    assert preview.helper_backup is None
    assert config_path.read_text(encoding="utf-8") == before
    assert not helper_path.exists()


def test_cline_control_plane_install_rejects_unreserved_helper_name(tmp_path: Path) -> None:
    config_path = tmp_path / "cline_mcp_settings.json"
    config_path.write_text('{"mcpServers":{}}\n', encoding="utf-8")

    try:
        plan_cline_control_plane_install(
            config_path=config_path,
            helper_path=tmp_path / ".mcp-multiplex" / "custom.sh",
        )
    except ControlPlaneInstallError as error:
        assert CLINE_CONTROL_HELPER in str(error)
    else:
        raise AssertionError("expected ControlPlaneInstallError")


def test_cline_control_plane_install_writes_mcp_remote_helper_and_backup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "multiplex.db"
    config_path = tmp_path / "cline_mcp_settings.json"
    helper_path = tmp_path / ".mcp-multiplex" / CLINE_CONTROL_HELPER
    config_path.write_text('{"mcpServers":{"context7":{"command":"npx"}}}\n', encoding="utf-8")
    connection = connect(db_path)

    result = install_cline_control_plane(
        connection,
        config_path=config_path,
        helper_path=helper_path,
    )

    text = config_path.read_text(encoding="utf-8")
    parsed_json = json.loads(text)
    hub = parsed_json["mcpServers"]["mcp_hub"]
    assert hub == {
        "command": result.helper_path,
        "args": [],
        "disabled": False,
        "autoApprove": [],
    }
    assert result.helper_path is not None
    helper_path = Path(result.helper_path)
    assert helper_path.exists()
    helper_text = helper_path.read_text(encoding="utf-8")
    assert "mcp-remote" in helper_text
    assert CODEX_CONTROL_TOKEN_ENV_VAR in helper_text
    assert result.backup is not None
    assert result.helper_backup is not None
    assert ConfigBackupStore(connection).show(result.backup.backup_id).bytes > 0
    assert ConfigBackupStore(connection).show(result.helper_backup.backup_id).bytes == 0
    parsed = parse_cline_config(config_path)
    observed_hub = next(entry for entry in parsed.observed_entries if entry.mount_name == "mcp_hub")
    assert observed_hub.url == MCP_HUB_URL
    assert observed_hub.headers_present == ["Authorization"]
    assert result.token is not None
    agent = AgentRegistry(connection).show(CLINE_AGENT_ID)
    assert agent.auth_token_ref == result.token.token_ref
    assert agent.certification_level == "certified"
    assert agent.config_paths[0].path == str(config_path.resolve())
    assert (
        AuthTokenStore(connection)
        .verify_local_token(
            result.token.token,
            required_scope=CONTROL_READ,
        )
        .subject_id
        == CLINE_AGENT_ID
    )
    assert result.token.token not in text
    assert result.token.token not in helper_text


def test_opencode_control_plane_install_dry_run_does_not_mutate(tmp_path: Path) -> None:
    config_path = tmp_path / "opencode.jsonc"
    before = '{"mcp":{"context7":{"type":"remote","url":"http://example.test/mcp"}}}\n'
    config_path.write_text(before, encoding="utf-8")

    preview = plan_opencode_control_plane_install(config_path=config_path)

    assert preview.agent_id == OPENCODE_AGENT_ID
    assert preview.env_var == CODEX_CONTROL_TOKEN_ENV_VAR
    assert preview.url == MCP_HUB_URL
    assert preview.would_change is True
    assert preview.backup is None
    assert config_path.read_text(encoding="utf-8") == before


def test_gemini_control_plane_install_dry_run_does_not_mutate(tmp_path: Path) -> None:
    config_path = tmp_path / "settings.json"
    before = '{"mcpServers":{"context7":{"httpUrl":"http://example.test/mcp"}}}\n'
    config_path.write_text(before, encoding="utf-8")

    preview = plan_gemini_control_plane_install(config_path=config_path)

    assert preview.agent_id == GEMINI_AGENT_ID
    assert preview.env_var == CODEX_CONTROL_TOKEN_ENV_VAR
    assert preview.url == MCP_HUB_URL
    assert preview.would_change is True
    assert preview.backup is None
    assert config_path.read_text(encoding="utf-8") == before


def test_gemini_control_plane_install_writes_env_header_template_and_backup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "multiplex.db"
    config_path = tmp_path / "settings.json"
    config_path.write_text(
        '{"mcpServers":{"context7":{"httpUrl":"http://example.test/mcp"}}}\n',
        encoding="utf-8",
    )
    connection = connect(db_path)

    result = install_gemini_control_plane(connection, config_path=config_path)

    text = config_path.read_text(encoding="utf-8")
    parsed_json = json.loads(text)
    hub = parsed_json["mcpServers"]["mcp_hub"]
    assert hub == {
        "httpUrl": MCP_HUB_URL,
        "headers": {"Authorization": f"Bearer ${CODEX_CONTROL_TOKEN_ENV_VAR}"},
        "trust": False,
    }
    assert result.backup is not None
    assert ConfigBackupStore(connection).show(result.backup.backup_id).bytes > 0
    parsed = parse_gemini_config(config_path)
    observed_hub = next(entry for entry in parsed.observed_entries if entry.mount_name == "mcp_hub")
    assert observed_hub.headers_present == ["Authorization"]
    assert result.token is not None
    agent = AgentRegistry(connection).show(GEMINI_AGENT_ID)
    assert agent.auth_token_ref == result.token.token_ref
    assert agent.certification_level == "certified"
    assert agent.config_paths[0].path == str(config_path.resolve())
    assert (
        AuthTokenStore(connection)
        .verify_local_token(
            result.token.token,
            required_scope=CONTROL_READ,
        )
        .subject_id
        == GEMINI_AGENT_ID
    )
    assert result.token.token not in text


def test_opencode_control_plane_install_writes_env_header_template_and_backup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "multiplex.db"
    config_path = tmp_path / "opencode.jsonc"
    config_path.write_text(
        """
        {
          // Existing JSONC comments can be parsed before rewrite.
          "mcp": {
            "context7": {"type": "remote", "url": "http://example.test/mcp"}
          }
        }
        """,
        encoding="utf-8",
    )
    connection = connect(db_path)

    result = install_opencode_control_plane(connection, config_path=config_path)

    text = config_path.read_text(encoding="utf-8")
    parsed_json = json.loads(text)
    hub = parsed_json["mcp"]["mcp_hub"]
    assert hub == {
        "type": "remote",
        "url": MCP_HUB_URL,
        "oauth": False,
        "headers": {"Authorization": f"Bearer {{env:{CODEX_CONTROL_TOKEN_ENV_VAR}}}"},
    }
    assert result.backup is not None
    assert ConfigBackupStore(connection).show(result.backup.backup_id).bytes > 0
    parsed = parse_opencode_config(config_path)
    observed_hub = next(entry for entry in parsed.observed_entries if entry.mount_name == "mcp_hub")
    assert observed_hub.headers_present == ["Authorization"]
    assert result.token is not None
    agent = AgentRegistry(connection).show(OPENCODE_AGENT_ID)
    assert agent.auth_token_ref == result.token.token_ref
    assert agent.certification_level == "certified"
    assert agent.config_paths[0].path == str(config_path.resolve())
    assert (
        AuthTokenStore(connection)
        .verify_local_token(
            result.token.token,
            required_scope=CONTROL_READ,
        )
        .subject_id
        == OPENCODE_AGENT_ID
    )
    assert result.token.token not in text


def test_control_plane_auth_capabilities_explain_safe_and_blocked_paths() -> None:
    capabilities = {item.agent_kind: item for item in control_plane_auth_capabilities()}

    assert list(capabilities) == ["codex", "claude_code", "gemini", "cline", "opencode"]
    assert capabilities["codex"].automatic_install_supported is True
    assert capabilities["codex"].auth_strategy == "bearer_token_env_var"
    assert capabilities["codex"].raw_token_storage_required is False

    assert capabilities["claude_code"].automatic_install_supported is True
    assert capabilities["claude_code"].auth_strategy == "headersHelper"
    assert capabilities["claude_code"].raw_token_storage_required is False

    assert capabilities["gemini"].automatic_install_supported is True
    assert capabilities["gemini"].auth_strategy == "env_header_template"
    assert capabilities["gemini"].raw_token_storage_required is False
    assert capabilities["cline"].automatic_install_supported is True
    assert capabilities["cline"].auth_strategy == "stdio_mcp_remote_helper"
    assert capabilities["cline"].raw_token_storage_required is False

    assert capabilities["opencode"].automatic_install_supported is True
    assert capabilities["opencode"].auth_strategy == "env_header_template"
    assert capabilities["opencode"].raw_token_storage_required is False

    assert control_plane_auth_capability("codex") == capabilities["codex"]
