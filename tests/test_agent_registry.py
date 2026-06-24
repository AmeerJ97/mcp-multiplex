from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.adapters import AgentConfigPath, AgentRegistry, AgentRegistryError
from mcp_multiplex.auth import CONTROL_READ, AuthTokenStore
from mcp_multiplex.storage import connect


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "multiplex.db")


def test_create_show_and_list_agent_registration(connection: sqlite3.Connection) -> None:
    registry = AgentRegistry(connection)

    created = registry.create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
        workspace_root="/workspace/project",
        config_paths=[
            AgentConfigPath(path="/home/user/.codex/config.toml", format="toml", precedence=10),
            AgentConfigPath(
                path="/workspace/project/.codex/config.toml",
                format="toml",
                precedence=20,
                is_project_shared=True,
            ),
        ],
        auth_token_ref="secretref:agent/codex",
        certification_level="certified",
    )

    shown = registry.show("agent_codex_user_default")
    listed = registry.list()
    assert created == shown
    assert listed == [shown]
    assert shown.agent_kind == "codex"
    assert shown.display_name == "Codex CLI"
    assert shown.workspace_root == "/workspace/project"
    assert shown.control_plane_mount == "mcp_hub"
    assert shown.auth_token_ref == "secretref:agent/codex"
    assert shown.certification_level == "certified"
    assert shown.can_auto_remediate is True
    assert [path.path for path in shown.config_paths] == [
        "/home/user/.codex/config.toml",
        "/workspace/project/.codex/config.toml",
    ]
    assert shown.config_paths[1].is_project_shared is True


def test_agent_records_are_durable_in_sqlite(connection: sqlite3.Connection) -> None:
    registry = AgentRegistry(connection)
    registry.create(
        agent_id="agent_claude_user_default",
        agent_kind="claude_code",
        display_name="Claude Code",
        config_paths=[AgentConfigPath(path="/home/user/.claude/config.json", format="json")],
    )

    row = connection.execute(
        """
        SELECT
          agents.agent_id,
          agents.agent_kind,
          agents.certification_level,
          agent_config_paths.path
        FROM agents
        JOIN agent_config_paths USING (agent_id)
        WHERE agents.agent_id = 'agent_claude_user_default'
        """
    ).fetchone()

    assert dict(row) == {
        "agent_id": "agent_claude_user_default",
        "agent_kind": "claude_code",
        "certification_level": "unverified",
        "path": "/home/user/.claude/config.json",
    }


def test_invalid_agent_kind_is_rejected(connection: sqlite3.Connection) -> None:
    registry = AgentRegistry(connection)

    with pytest.raises(AgentRegistryError, match="unsupported agent_kind"):
        registry.create(
            agent_id="agent_cursor_user_default",
            agent_kind="cursor",
            display_name="Cursor",
        )


def test_invalid_certification_level_is_rejected(connection: sqlite3.Connection) -> None:
    registry = AgentRegistry(connection)

    with pytest.raises(AgentRegistryError, match="unsupported certification_level"):
        registry.create(
            agent_id="agent_codex_user_default",
            agent_kind="codex",
            display_name="Codex",
            certification_level="trusted",
        )


def test_certification_level_controls_future_auto_remediation_flag(
    connection: sqlite3.Connection,
) -> None:
    registry = AgentRegistry(connection)
    certified = registry.create(
        agent_id="agent_gemini_user_default",
        agent_kind="gemini",
        display_name="Gemini CLI",
        certification_level="certified",
    )
    best_effort = registry.create(
        agent_id="agent_opencode_user_default",
        agent_kind="opencode",
        display_name="OpenCode",
        certification_level="best_effort",
    )

    assert certified.can_auto_remediate is True
    assert best_effort.can_auto_remediate is False


def test_registry_issues_and_exchanges_agent_registration_token(
    connection: sqlite3.Connection,
) -> None:
    registry = AgentRegistry(connection)
    registry.create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )

    registration = registry.issue_registration_token(
        "agent_codex_user_default",
        scopes=[CONTROL_READ],
    )
    issued = registry.exchange_registration_token(registration.token)

    shown = registry.show("agent_codex_user_default")
    assert issued.subject_id == "agent_codex_user_default"
    assert shown.auth_token_ref == issued.token_ref
    assert (
        AuthTokenStore(connection)
        .verify_local_token(
            issued.token,
            required_scope=CONTROL_READ,
        )
        .subject_id
        == "agent_codex_user_default"
    )


def test_invalid_config_path_is_rejected(connection: sqlite3.Connection) -> None:
    registry = AgentRegistry(connection)

    with pytest.raises(AgentRegistryError, match="unsupported config path format"):
        registry.create(
            agent_id="agent_cline_user_default",
            agent_kind="cline",
            display_name="Cline",
            config_paths=[AgentConfigPath(path="/tmp/cline.ini", format="ini")],
        )


def test_show_missing_agent_raises_key_error(connection: sqlite3.Connection) -> None:
    registry = AgentRegistry(connection)

    with pytest.raises(KeyError):
        registry.show("agent_missing")
