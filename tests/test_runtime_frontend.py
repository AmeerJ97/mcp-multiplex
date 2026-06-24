from __future__ import annotations

import json
import sqlite3
import sys
import threading
from collections.abc import Generator, Mapping
from datetime import datetime
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from mcp_multiplex.adapters import AgentRegistry
from mcp_multiplex.auth import CONTROL_READ, AuthTokenStore
from mcp_multiplex.catalog import CatalogStore
from mcp_multiplex.credentials import CredentialRefStore
from mcp_multiplex.daemon import (
    MCP_PROTOCOL_VERSION_HEADER,
    MCP_SESSION_HEADER,
    build_server,
)
from mcp_multiplex.observability import EventStore
from mcp_multiplex.runtime import RuntimeBackendStore, RuntimeFrontendSessionStore
from mcp_multiplex.schemas import CatalogEntry
from mcp_multiplex.storage import connect
from tests.test_schema_models import catalog_entry_payload


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "multiplex.db")
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    CatalogStore(connection).upsert(CatalogEntry.from_dict(fake_stdio_catalog_entry_payload()))
    return connection


@pytest.fixture
def runtime_server(connection: sqlite3.Connection) -> Generator[ThreadingHTTPServer]:
    server = build_server(port=0, connection=connection)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_initialize_creates_frontend_session(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    payload: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": "init-1",
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"},
    }

    response, body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        payload,
        headers={
            "X-MCP-Multiplex-Agent-ID": "agent_codex_user_default",
            "X-MCP-Multiplex-Workspace-Root": "/workspace/project",
        },
    )

    session_id = response.headers[MCP_SESSION_HEADER]
    assert response.status == 200
    assert session_id.startswith("fs_")
    assert body == {
        "jsonrpc": "2.0",
        "id": "init-1",
        "result": {
            "capabilities": {"tools": {}},
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "fake-stdio", "version": "0.1.0"},
        },
    }
    sessions = RuntimeFrontendSessionStore(connection).list(server_name="context7")
    assert len(sessions) == 1
    assert sessions[0].frontend_session_id == session_id
    assert sessions[0].agent_id == "agent_codex_user_default"
    assert sessions[0].workspace_root == "/workspace/project"
    assert sessions[0].protocol_version == "2025-06-18"
    assert sessions[0].backend_id is not None

    backends = RuntimeBackendStore(connection).list()
    assert len(backends) == 1
    assert backends[0].backend_id == sessions[0].backend_id
    assert backends[0].catalog_id == "srv_context7"
    assert backends[0].runtime_pool_key == "global:catalog:srv_context7"
    assert backends[0].state == "hot"
    assert backends[0].pid is not None
    assert backends[0].backend_initialize_count == 1
    assert backends[0].frontend_session_count == 1
    assert backends[0].initialize_result_json is not None


def test_global_shareability_reuses_hot_stdio_backend(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    first_response, first_body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "init-a", "method": "initialize"},
    )
    second_response, second_body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "init-b", "method": "initialize"},
    )

    first_session_id = first_response.headers[MCP_SESSION_HEADER]
    second_session_id = second_response.headers[MCP_SESSION_HEADER]
    assert first_session_id != second_session_id
    assert first_body["id"] == "init-a"
    assert second_body["id"] == "init-b"

    sessions = RuntimeFrontendSessionStore(connection).list(server_name="context7")
    assert len(sessions) == 2
    assert sessions[0].backend_id == sessions[1].backend_id

    backends = RuntimeBackendStore(connection).list()
    assert len(backends) == 1
    assert backends[0].runtime_pool_key == "global:catalog:srv_context7"
    assert backends[0].backend_initialize_count == 1
    assert backends[0].frontend_session_count == 2
    assert [
        event.payload["backend_id"]
        for event in EventStore(connection).query(event_type="runtime.backend_reused")
    ] == [backends[0].backend_id]


def test_tools_requests_forward_to_stdio_backend(
    runtime_server: ThreadingHTTPServer,
) -> None:
    initialize_response, initialize_body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {
            "jsonrpc": "2.0",
            "id": "init-tools",
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    initialize_result = initialize_body["result"]
    assert isinstance(initialize_result, dict)
    assert initialize_result["serverInfo"] == {"name": "fake-stdio", "version": "0.1.0"}
    session_id = initialize_response.headers[MCP_SESSION_HEADER]

    response, body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "tools-1", "method": "tools/list"},
        headers={MCP_SESSION_HEADER: session_id},
    )

    assert response.status == 200
    assert body == {
        "jsonrpc": "2.0",
        "id": "tools-1",
        "result": {
            "tools": [
                {
                    "description": "Return pong.",
                    "inputSchema": {"properties": {}, "type": "object"},
                    "name": "ping",
                }
            ]
        },
    }

    response, body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        },
        headers={MCP_SESSION_HEADER: session_id},
    )

    assert response.status == 200
    assert body == {
        "jsonrpc": "2.0",
        "id": "call-1",
        "result": {"content": [{"text": "pong", "type": "text"}], "isError": False},
    }

    response, body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {
            "jsonrpc": "2.0",
            "id": "frontend-id-visible",
            "method": "tools/call",
            "params": {"name": "backend_request_id", "arguments": {}},
        },
        headers={MCP_SESSION_HEADER: session_id},
    )

    assert response.status == 200
    assert body["id"] == "frontend-id-visible"
    result = body["result"]
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list)
    first_item = content[0]
    assert isinstance(first_item, dict)
    assert isinstance(first_item["text"], str)
    assert first_item["text"].startswith("hb_")
    assert first_item["text"] != "frontend-id-visible"


def test_stdio_backend_resolves_required_env_at_startup_with_redacted_event(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_value = "runtime-fixture-value"
    monkeypatch.setenv("SERVICE_TOKEN", env_value)
    CatalogStore(connection).upsert(
        CatalogEntry.from_dict(
            fake_stdio_catalog_entry_payload(required_env_names=["SERVICE_TOKEN"])
        )
    )
    CredentialRefStore(connection).create(
        catalog_id="srv_context7",
        name="SERVICE_TOKEN",
        source_kind="env",
        source_ref="secretref:env/SERVICE_TOKEN",
    )

    initialize_response, _ = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "init-env", "method": "initialize"},
    )
    session_id = initialize_response.headers[MCP_SESSION_HEADER]

    response, body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {
            "jsonrpc": "2.0",
            "id": "env-check",
            "method": "tools/call",
            "params": {"name": "env_present", "arguments": {"name": "SERVICE_TOKEN"}},
        },
        headers={MCP_SESSION_HEADER: session_id},
    )

    assert response.status == 200
    assert body["result"] == {
        "content": [{"text": "present", "type": "text"}],
        "isError": False,
    }
    event = EventStore(connection).query(event_type="runtime.backend_started")[0]
    assert event.payload["resolved_env_names"] == ["SERVICE_TOKEN"]
    assert event.payload["resolved_count"] == 1
    event_row = connection.execute(
        "SELECT payload_json FROM events WHERE event_id = ?",
        (event.event.event_id,),
    ).fetchone()
    assert env_value not in str(event_row["payload_json"])
    assert env_value not in json.dumps(body, sort_keys=True)


def test_stdio_backend_startup_blocks_missing_required_credential(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    CatalogStore(connection).upsert(
        CatalogEntry.from_dict(
            fake_stdio_catalog_entry_payload(required_env_names=["SERVICE_TOKEN"])
        )
    )

    response, body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "init-missing-env", "method": "initialize"},
        expected_status=502,
    )

    assert response.status == 502
    assert body["error"] == {
        "code": -32003,
        "message": (
            "stdio backend initialization failed: "
            "required credentials are not configured: SERVICE_TOKEN"
        ),
    }
    assert RuntimeBackendStore(connection).list() == []


def test_remote_http_backend_session_is_forwarded(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    provider = FakeRemoteMCPProvider()
    try:
        CatalogStore(connection).upsert(
            CatalogEntry.from_dict(remote_http_catalog_entry_payload(provider.url("/mcp")))
        )

        initialize_response, initialize_body = post_json(
            runtime_server,
            "/servers/remote-test/mcp",
            {
                "jsonrpc": "2.0",
                "id": "remote-init",
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
        )
        frontend_session_id = initialize_response.headers[MCP_SESSION_HEADER]

        assert initialize_body == {
            "jsonrpc": "2.0",
            "id": "remote-init",
            "result": {
                "capabilities": {"tools": {}},
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "fake-remote-http", "version": "0.1.0"},
            },
        }
        frontend_session = RuntimeFrontendSessionStore(connection).show(frontend_session_id)
        assert frontend_session.backend_id is not None
        backend = RuntimeBackendStore(connection).show(frontend_session.backend_id)
        assert backend.catalog_id == "srv_remote_test"
        assert backend.pid is None
        assert backend.state == "hot"
        assert backend.runtime_pool_key.startswith("isolated:catalog:srv_remote_test:")

        response, body = post_json(
            runtime_server,
            "/servers/remote-test/mcp",
            {
                "jsonrpc": "2.0",
                "id": "remote-call",
                "method": "tools/call",
                "params": {"name": "remote_ping", "arguments": {}},
            },
            headers={MCP_SESSION_HEADER: frontend_session_id},
        )

        assert response.status == 200
        assert body == {
            "jsonrpc": "2.0",
            "id": "remote-call",
            "result": {"content": [{"text": "remote pong", "type": "text"}], "isError": False},
        }
        assert provider.request_sessions == [None, "remote-session-1"]

        delete_response, delete_body = delete_session(
            runtime_server, "/servers/remote-test/mcp", frontend_session_id
        )

        assert delete_response.status == 200
        assert delete_body == {"frontend_session_id": frontend_session_id, "ok": True}
        assert RuntimeBackendStore(connection).show(frontend_session.backend_id).state == "stopped"
        assert provider.deleted_sessions == ["remote-session-1"]
        assert all(
            isinstance(request_id, str) and request_id.startswith("hb_")
            for request_id in provider.request_ids
        )
        assert "remote-init" not in provider.request_ids
        assert "remote-call" not in provider.request_ids
    finally:
        provider.close()


def test_isolated_remote_backend_creates_separate_backends(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    provider = FakeRemoteMCPProvider()
    try:
        CatalogStore(connection).upsert(
            CatalogEntry.from_dict(remote_http_catalog_entry_payload(provider.url("/mcp")))
        )

        first_response, _ = post_json(
            runtime_server,
            "/servers/remote-test/mcp",
            {"jsonrpc": "2.0", "id": "remote-init-a", "method": "initialize"},
        )
        second_response, _ = post_json(
            runtime_server,
            "/servers/remote-test/mcp",
            {"jsonrpc": "2.0", "id": "remote-init-b", "method": "initialize"},
        )

        first_session = RuntimeFrontendSessionStore(connection).show(
            first_response.headers[MCP_SESSION_HEADER]
        )
        second_session = RuntimeFrontendSessionStore(connection).show(
            second_response.headers[MCP_SESSION_HEADER]
        )
        assert first_session.backend_id is not None
        assert second_session.backend_id is not None
        assert first_session.backend_id != second_session.backend_id

        remote_backends = [
            backend
            for backend in RuntimeBackendStore(connection).list()
            if backend.catalog_id == "srv_remote_test"
        ]
        assert len(remote_backends) == 2
        assert {backend.frontend_session_count for backend in remote_backends} == {1}
        assert {backend.backend_initialize_count for backend in remote_backends} == {1}
        assert len({backend.runtime_pool_key for backend in remote_backends}) == 2
        assert all(
            backend.runtime_pool_key.startswith("isolated:catalog:srv_remote_test:")
            for backend in remote_backends
        )
        assert provider.request_sessions == [None, None]
    finally:
        provider.close()


def test_crashed_backend_returns_clear_error_then_restarts_lazily(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    initialize_response, _ = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "init-crash", "method": "initialize"},
    )
    session_id = initialize_response.headers[MCP_SESSION_HEADER]
    session = RuntimeFrontendSessionStore(connection).show(session_id)
    assert session.backend_id is not None

    runtime_server.stdio_backends.close(session.backend_id)  # type: ignore[attr-defined]

    response, body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "after-crash", "method": "tools/list"},
        headers={MCP_SESSION_HEADER: session_id},
        expected_status=502,
    )
    assert response.status == 502
    error = body["error"]
    assert isinstance(error, dict)
    assert error["code"] == -32003
    assert "not registered" in str(error["message"])
    assert RuntimeBackendStore(connection).show(session.backend_id).state == "crashed"

    response, body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "after-restart", "method": "tools/list"},
        headers={MCP_SESSION_HEADER: session_id},
    )

    assert response.status == 200
    assert body["id"] == "after-restart"
    backend = RuntimeBackendStore(connection).show(session.backend_id)
    assert backend.state == "hot"
    assert backend.backend_initialize_count == 2
    assert [event.event.event_type for event in EventStore(connection).query()] == [
        "runtime.backend_started",
        "runtime.backend_crashed",
        "runtime.backend_restarted",
    ]


def test_cancellation_notification_is_acknowledged_and_emits_event(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    initialize_response, _ = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "init-cancel", "method": "initialize"},
    )
    session_id = initialize_response.headers[MCP_SESSION_HEADER]

    response, body = post_notification(
        runtime_server,
        "/servers/context7/mcp",
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "frontend-request"},
        },
        headers={MCP_SESSION_HEADER: session_id},
        expected_status=202,
    )

    assert response.status == 202
    assert body == b""
    events = EventStore(connection).query(event_type="runtime.request_cancelled")
    assert len(events) == 1
    assert events[0].payload["frontend_session_id"] == session_id
    assert events[0].payload["params"] == {"requestId": "frontend-request"}


def test_data_plane_lifecycle_notification_is_forwarded_with_empty_ack(
    runtime_server: ThreadingHTTPServer,
) -> None:
    initialize_response, _ = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "init-notification", "method": "initialize"},
    )

    response, body = post_notification(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={MCP_SESSION_HEADER: initialize_response.headers[MCP_SESSION_HEADER]},
    )

    assert response.status == 202
    assert body == b""


def test_idle_reaper_closes_unused_hot_backend_and_emits_event(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    initialize_response, _ = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "init-idle", "method": "initialize"},
    )
    session_id = initialize_response.headers[MCP_SESSION_HEADER]
    session = RuntimeFrontendSessionStore(connection).show(session_id)
    assert session.backend_id is not None
    old_timestamp = "2026-06-20T00:00:00Z"
    with connection:
        connection.execute(
            """
            UPDATE runtime_backends
            SET frontend_session_count = 0,
                last_used_at = ?,
                state = 'hot'
            WHERE backend_id = ?
            """,
            (old_timestamp, session.backend_id),
        )

    reaped = runtime_server.reap_idle_backends(  # type: ignore[attr-defined]
        now=datetime.fromisoformat("2026-06-20T00:20:00+00:00")
    )

    assert reaped == [session.backend_id]
    assert RuntimeBackendStore(connection).show(session.backend_id).state == "stopped"
    events = EventStore(connection).query(event_type="runtime.backend_reaped")
    assert len(events) == 1
    assert events[0].payload["backend_id"] == session.backend_id


def test_non_initialize_requires_known_frontend_session(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    session_id = (
        RuntimeFrontendSessionStore(connection).create(server_name="context7").frontend_session_id
    )

    response, body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={MCP_SESSION_HEADER: session_id},
        expected_status=501,
    )

    assert response.status == 501
    assert body["error"] == {
        "code": -32002,
        "message": "frontend session is not attached to a backend",
    }
    assert RuntimeFrontendSessionStore(connection).show(session_id).last_seen_at is not None


def test_non_initialize_without_session_is_rejected(
    runtime_server: ThreadingHTTPServer,
) -> None:
    response, body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        expected_status=400,
    )

    assert response.status == 400
    assert body["error"] == {"code": -32001, "message": "missing Mcp-Session-Id header"}


def test_delete_frontend_session(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    initialize_response, _ = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "init-delete", "method": "initialize"},
    )
    session_id = initialize_response.headers[MCP_SESSION_HEADER]
    session = RuntimeFrontendSessionStore(connection).show(session_id)
    assert session.backend_id is not None

    response, body = delete_session(runtime_server, "/servers/context7/mcp", session_id)

    assert response.status == 200
    assert body == {"frontend_session_id": session_id, "ok": True}
    assert RuntimeFrontendSessionStore(connection).list(server_name="context7") == []
    assert RuntimeBackendStore(connection).show(session.backend_id).state == "stopped"


def test_unknown_server_returns_json_rpc_error(runtime_server: ThreadingHTTPServer) -> None:
    response, body = post_json(
        runtime_server,
        "/servers/missing/mcp",
        {"jsonrpc": "2.0", "id": "init", "method": "initialize"},
        expected_status=404,
    )

    assert response.status == 404
    assert body["error"] == {"code": -32004, "message": "unknown MCP server: missing"}


def test_mcp_hub_control_plane_exposes_authenticated_mcp_tools(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    token = (
        AuthTokenStore(connection)
        .issue_local_token(
            subject_type="agent",
            subject_id="agent_codex_user_default",
            scopes=[CONTROL_READ],
        )
        .token
    )

    initialize_response, initialize_body = post_json(
        runtime_server,
        "/servers/mcp_hub/mcp",
        {
            "jsonrpc": "2.0",
            "id": "hub-init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        headers={"X-MCP-Governor-Agent-ID": "agent_codex_user_default"},
    )
    session_id = initialize_response.headers[MCP_SESSION_HEADER]

    assert initialize_body == {
        "jsonrpc": "2.0",
        "id": "hub-init",
        "result": {
            "capabilities": {"tools": {}},
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "mcp_hub", "version": "0.1.0"},
        },
    }
    assert RuntimeFrontendSessionStore(connection).show(session_id).server_name == "mcp_hub"

    list_response, list_body = post_json(
        runtime_server,
        "/servers/mcp_hub/mcp",
        {"jsonrpc": "2.0", "id": "hub-tools", "method": "tools/list"},
        headers={MCP_SESSION_HEADER: session_id, "Authorization": f"Bearer {token}"},
    )

    assert list_response.status == 200
    list_result = list_body["result"]
    assert isinstance(list_result, dict)
    tools = list_result["tools"]
    assert isinstance(tools, list)
    tool_names = [tool["name"] for tool in tools if isinstance(tool, dict)]
    assert "self_check" in tool_names
    assert "apply" not in tool_names
    assert "rollback" not in tool_names

    call_response, call_body = post_json(
        runtime_server,
        "/servers/mcp_hub/mcp",
        {
            "jsonrpc": "2.0",
            "id": "hub-self-check",
            "method": "tools/call",
            "params": {"name": "self_check", "arguments": {}},
        },
        headers={MCP_SESSION_HEADER: session_id, "Authorization": f"Bearer {token}"},
    )

    assert call_response.status == 200
    call_result = call_body["result"]
    assert isinstance(call_result, dict)
    assert call_result["isError"] is False
    content = call_result["content"]
    assert isinstance(content, list)
    content_item = content[0]
    assert isinstance(content_item, dict)
    payload = json.loads(str(content_item["text"]))
    assert payload["kind"] == "MCPHubSelfCheck"
    assert payload["agent_id"] == "agent_codex_user_default"
    assert payload["destructive_actions"] == []


def test_mcp_hub_control_plane_rejects_tools_without_bearer_token(
    runtime_server: ThreadingHTTPServer,
) -> None:
    initialize_response, _ = post_json(
        runtime_server,
        "/servers/mcp_hub/mcp",
        {"jsonrpc": "2.0", "id": "hub-init", "method": "initialize"},
    )

    response, body = post_json(
        runtime_server,
        "/servers/mcp_hub/mcp",
        {"jsonrpc": "2.0", "id": "hub-tools", "method": "tools/list"},
        headers={MCP_SESSION_HEADER: initialize_response.headers[MCP_SESSION_HEADER]},
        expected_status=401,
    )

    assert response.status == 401
    assert body["error"] == {"code": -32007, "message": "missing Authorization bearer token"}


def test_mcp_hub_control_plane_acknowledges_lifecycle_notifications_without_auth(
    runtime_server: ThreadingHTTPServer,
) -> None:
    initialize_response, _ = post_json(
        runtime_server,
        "/servers/mcp_hub/mcp",
        {"jsonrpc": "2.0", "id": "hub-init", "method": "initialize"},
    )
    session_id = initialize_response.headers[MCP_SESSION_HEADER]

    response, body = post_notification(
        runtime_server,
        "/servers/mcp_hub/mcp",
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={MCP_SESSION_HEADER: session_id},
        expected_status=202,
    )

    assert response.status == 202
    assert body == b""


def test_mcp_hub_control_plane_rejects_mismatched_protocol_version_header(
    runtime_server: ThreadingHTTPServer,
) -> None:
    initialize_response, _ = post_json(
        runtime_server,
        "/servers/mcp_hub/mcp",
        {
            "jsonrpc": "2.0",
            "id": "hub-init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )

    response, body = post_json(
        runtime_server,
        "/servers/mcp_hub/mcp",
        {"jsonrpc": "2.0", "id": "hub-tools", "method": "tools/list"},
        headers={
            MCP_SESSION_HEADER: initialize_response.headers[MCP_SESSION_HEADER],
            MCP_PROTOCOL_VERSION_HEADER: "2025-11-25",
        },
        expected_status=400,
    )

    assert response.status == 400
    assert body["error"] == {
        "code": -32600,
        "message": (
            "MCP-Protocol-Version does not match the initialized "
            "session protocol version 2025-06-18"
        ),
    }


def test_mcp_hub_control_plane_negotiates_latest_supported_version(
    runtime_server: ThreadingHTTPServer,
    connection: sqlite3.Connection,
) -> None:
    response, body = post_json(
        runtime_server,
        "/servers/mcp_hub/mcp",
        {
            "jsonrpc": "2.0",
            "id": "hub-init-future",
            "method": "initialize",
            "params": {"protocolVersion": "2099-01-01"},
        },
    )

    result = body["result"]
    assert isinstance(result, dict)
    assert result["protocolVersion"] == "2025-11-25"
    session = RuntimeFrontendSessionStore(connection).show(response.headers[MCP_SESSION_HEADER])
    assert session.protocol_version == "2025-11-25"


def test_delete_rejects_mismatched_protocol_version_header(
    runtime_server: ThreadingHTTPServer,
) -> None:
    initialize_response, _ = post_json(
        runtime_server,
        "/servers/mcp_hub/mcp",
        {
            "jsonrpc": "2.0",
            "id": "hub-init-delete-version",
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )

    request = Request(
        url(runtime_server, "/servers/mcp_hub/mcp"),
        method="DELETE",
        headers={
            MCP_SESSION_HEADER: initialize_response.headers[MCP_SESSION_HEADER],
            MCP_PROTOCOL_VERSION_HEADER: "2025-11-25",
        },
    )
    try:
        response = urlopen(request, timeout=2)
    except HTTPError as error:
        response = error
    body = json.loads(response.read().decode("utf-8"))

    assert response.status == 400
    assert body["error"]["code"] == -32600


def test_browser_origin_header_is_denied_before_runtime_dispatch(
    runtime_server: ThreadingHTTPServer,
) -> None:
    response, body = post_json(
        runtime_server,
        "/servers/context7/mcp",
        {"jsonrpc": "2.0", "id": "origin-denied", "method": "initialize"},
        headers={"Origin": "https://evil.example"},
        expected_status=403,
    )

    assert response.status == 403
    assert body["error"] == {
        "code": -32006,
        "message": "browser origin is not allowed: https://evil.example",
    }


def test_health_endpoint_still_works(runtime_server: ThreadingHTTPServer) -> None:
    with urlopen(url(runtime_server, "/healthz"), timeout=2) as response:
        body = json.loads(response.read().decode("utf-8"))

    assert response.status == 200
    assert body["kind"] == "MCPMultiplexHealth"


def post_json(
    server: ThreadingHTTPServer,
    path: str,
    payload: dict[str, object],
    *,
    headers: Mapping[str, str] | None = None,
    expected_status: int = 200,
) -> tuple[HTTPResponse, dict[str, object]]:
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        url(server, path),
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        response = urlopen(request, timeout=2)
    except HTTPError as error:
        response = error
    assert response.status == expected_status
    return response, json.loads(response.read().decode("utf-8"))


def post_notification(
    server: ThreadingHTTPServer,
    path: str,
    payload: dict[str, object],
    *,
    headers: Mapping[str, str] | None = None,
    expected_status: int = 202,
) -> tuple[HTTPResponse, bytes]:
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        url(server, path),
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        response = urlopen(request, timeout=2)
    except HTTPError as error:
        response = error
    assert response.status == expected_status
    return response, response.read()


def delete_session(
    server: ThreadingHTTPServer,
    path: str,
    session_id: str,
) -> tuple[HTTPResponse, dict[str, object]]:
    request = Request(
        url(server, path),
        method="DELETE",
        headers={MCP_SESSION_HEADER: session_id},
    )
    response = urlopen(request, timeout=2)
    return response, json.loads(response.read().decode("utf-8"))


def url(server: ThreadingHTTPServer, path: str) -> str:
    return f"http://127.0.0.1:{int(server.server_address[1])}{path}"


def fake_stdio_catalog_entry_payload(
    *, required_env_names: list[str] | None = None
) -> dict[str, object]:
    payload = catalog_entry_payload()
    transport = payload["transport"]
    assert isinstance(transport, dict)
    backend = transport["backend"]
    assert isinstance(backend, dict)
    backend["command"] = sys.executable
    backend["args"] = [str(Path(__file__).parent / "fixtures" / "runtime" / "fake_stdio_mcp.py")]
    backend["env"] = required_env_names or []
    return payload


def remote_http_catalog_entry_payload(backend_url: str) -> dict[str, object]:
    payload = catalog_entry_payload()
    payload["catalog_id"] = "srv_remote_test"
    payload["name"] = "remote-test"
    payload["canonical_name"] = "test.remote"
    payload["family_id"] = "remote-test"
    payload["variant_name"] = "fake_http"
    payload["display_label"] = "Remote Test"
    payload["aliases"] = ["remote-test"]
    transport = payload["transport"]
    assert isinstance(transport, dict)
    transport["hub_path"] = "/servers/remote-test/mcp"
    backend = transport["backend"]
    assert isinstance(backend, dict)
    backend["type"] = "streamable_http"
    backend["command"] = None
    backend["args"] = []
    backend["url"] = backend_url
    runtime = payload["runtime"]
    assert isinstance(runtime, dict)
    runtime["shareability"] = "isolated_per_frontend_session"
    runtime["concurrency"] = "serialized"
    return payload


class FakeRemoteMCPProvider:
    def __init__(self) -> None:
        self.request_sessions: list[str | None] = []
        self.request_ids: list[object] = []
        self.deleted_sessions: list[str] = []
        self.initialize_count = 0
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{int(self.server.server_address[1])}{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/mcp":
                    self.send_error(404, "not found")
                    return
                payload = self._read_json()
                request_id = payload.get("id") if isinstance(payload, dict) else None
                method = payload.get("method") if isinstance(payload, dict) else None
                session_id = self.headers.get(MCP_SESSION_HEADER)
                provider.request_ids.append(request_id)
                provider.request_sessions.append(session_id)
                if method == "initialize":
                    provider.initialize_count += 1
                    backend_session_id = f"remote-session-{provider.initialize_count}"
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {"tools": {}},
                                "serverInfo": {
                                    "name": "fake-remote-http",
                                    "version": "0.1.0",
                                },
                            },
                        },
                        headers={MCP_SESSION_HEADER: backend_session_id},
                    )
                    return
                if session_id != "remote-session-1":
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32001, "message": "missing backend session"},
                        },
                        status=400,
                    )
                    return
                if method == "tools/call":
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {
                                "content": [{"type": "text", "text": "remote pong"}],
                                "isError": False,
                            },
                        }
                    )
                    return
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": "method not found"},
                    },
                    status=404,
                )

            def do_DELETE(self) -> None:
                session_id = self.headers.get(MCP_SESSION_HEADER)
                if session_id is not None:
                    provider.deleted_sessions.append(session_id)
                self._send_json({"ok": True})

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _read_json(self) -> dict[str, object]:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length)
                payload = json.loads(raw_body.decode("utf-8"))
                assert isinstance(payload, dict)
                return payload

            def _send_json(
                self,
                payload: dict[str, object],
                *,
                status: int = 200,
                headers: Mapping[str, str] | None = None,
            ) -> None:
                body = json.dumps(payload, sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        return Handler
