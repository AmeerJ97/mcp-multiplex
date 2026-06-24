from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.adapters import AgentRegistry
from mcp_multiplex.auth import CONTROL_MUTATE, CONTROL_READ, AuthTokenStore
from mcp_multiplex.catalog import CatalogStore
from mcp_multiplex.control_mcp import TOOL_NAMES, ControlMCPError, ControlMCPServer
from mcp_multiplex.credentials import CredentialRefStore
from mcp_multiplex.runtime import RuntimeBackendStore
from mcp_multiplex.schemas import CatalogEntry
from mcp_multiplex.storage import connect, migrate
from tests.test_schema_models import catalog_entry_payload


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "multiplex.db")
    migrate(connection)
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    AgentRegistry(connection).create(
        agent_id="agent_claude_user_default",
        agent_kind="claude_code",
        display_name="Claude Code",
    )
    CatalogStore(connection).upsert(CatalogEntry.from_dict(catalog_entry_payload()))
    _insert_plan(connection, "plan_codex", "agent_codex_user_default")
    _insert_plan(connection, "plan_claude", "agent_claude_user_default")
    return connection


@pytest.fixture
def agent_token(connection: sqlite3.Connection) -> str:
    return (
        AuthTokenStore(connection)
        .issue_local_token(
            subject_type="agent",
            subject_id="agent_codex_user_default",
            scopes=[CONTROL_READ],
        )
        .token
    )


def test_control_mcp_tool_list_is_read_only(connection: sqlite3.Connection) -> None:
    tools = ControlMCPServer(connection).list_tools()

    assert [tool["name"] for tool in tools] == list(TOOL_NAMES)
    assert "apply" not in {tool["name"] for tool in tools}
    assert "rollback" not in {tool["name"] for tool in tools}
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)


def test_control_mcp_requires_agent_scoped_read_token(connection: sqlite3.Connection) -> None:
    server = ControlMCPServer(connection)
    operator_token = (
        AuthTokenStore(connection)
        .issue_local_token(
            subject_type="operator",
            subject_id="local",
            scopes=[CONTROL_READ],
        )
        .token
    )
    mutate_only_token = (
        AuthTokenStore(connection)
        .issue_local_token(
            subject_type="agent",
            subject_id="agent_codex_user_default",
            scopes=[CONTROL_MUTATE],
        )
        .token
    )

    with pytest.raises(ControlMCPError, match="requires an auth token"):
        server.call_tool("status", {}, auth_token=None)
    with pytest.raises(ControlMCPError, match="agent-scoped"):
        server.call_tool("status", {}, auth_token=operator_token)
    with pytest.raises(ControlMCPError, match="missing required scope"):
        server.call_tool("status", {}, auth_token=mutate_only_token)


def test_self_check_returns_agent_scoped_status_and_plan_ids(
    connection: sqlite3.Connection,
    agent_token: str,
) -> None:
    response = ControlMCPServer(connection).call_tool("self_check", {}, auth_token=agent_token)

    assert response["kind"] == "MCPHubSelfCheck"
    assert response["agent_id"] == "agent_codex_user_default"
    assert response["plan_ids"] == ["plan_codex"]
    assert response["destructive_actions"] == []
    assert response["destructive_actions_require_approval"] is True


def test_plan_tools_are_scoped_to_invoking_agent(
    connection: sqlite3.Connection,
    agent_token: str,
) -> None:
    server = ControlMCPServer(connection)

    listed = server.call_tool("plan_list", {}, auth_token=agent_token)
    shown = server.call_tool("plan_show", {"plan_id": "plan_codex"}, auth_token=agent_token)

    assert [plan["plan_id"] for plan in listed["plans"]] == ["plan_codex"]
    assert shown["plan"]["plan_id"] == "plan_codex"
    with pytest.raises(ControlMCPError, match="not scoped"):
        server.call_tool("plan_show", {"plan_id": "plan_claude"}, auth_token=agent_token)


def test_proxy_runtime_credential_and_catalog_tools(
    connection: sqlite3.Connection,
    agent_token: str,
) -> None:
    RuntimeBackendStore(connection).create_starting(
        catalog_id="srv_context7",
        runtime_pool_key="global:catalog:srv_context7",
        pid=123,
    )
    CredentialRefStore(connection).create(
        catalog_id="srv_context7",
        name="SERVICE_TOKEN",
        source_kind="env",
        source_ref="secretref:env/SERVICE_TOKEN",
    )
    server = ControlMCPServer(connection)

    proxy = server.call_tool("proxy_url", {"server_name": "context7"}, auth_token=agent_token)
    runtime = server.call_tool(
        "runtime_status", {"server_name": "context7"}, auth_token=agent_token
    )
    credentials = server.call_tool("credential_status", {}, auth_token=agent_token)
    catalog = server.call_tool("catalog_search", {"query": "context7"}, auth_token=agent_token)

    assert proxy["url"] == "http://127.0.0.1:30000/servers/context7/mcp"
    assert runtime["backends"][0]["catalog_id"] == "srv_context7"
    assert credentials["summary"]["blockers"][0]["name"] == "SERVICE_TOKEN"
    assert "secretref" not in json.dumps(credentials, sort_keys=True)
    assert catalog["entries"][0]["catalog_id"] == "srv_context7"


def _insert_plan(connection: sqlite3.Connection, plan_id: str, agent_id: str) -> None:
    connection.execute(
        """
        INSERT INTO remediation_plans (
          plan_id,
          schema_version,
          plan_type,
          status,
          agent_id,
          target_path,
          observed_entry_id,
          catalog_id,
          policy_json,
          diff_format,
          diff_text,
          expected_preimage_hash,
          rollback_strategy,
          risk_json,
          created_at
        )
        VALUES (?, 1, 'rewrite_known_direct', 'pending_approval', ?,
                '/tmp/config.toml', NULL, 'srv_context7', ?, 'unified', '',
                'sha256:preimage', 'restore_backup', ?, '2026-06-20T00:00:00Z')
        """,
        (
            plan_id,
            agent_id,
            json.dumps({"approval_required": True, "approval_reason": "review"}),
            json.dumps({"tier": "normal"}),
        ),
    )
    connection.commit()
