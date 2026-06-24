"""Daemon entrypoint for the local convergence service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from mcp_multiplex.auth import CONTROL_READ, AuthContext, AuthError, AuthTokenStore
from mcp_multiplex.catalog import CatalogStore
from mcp_multiplex.credentials import (
    CredentialError,
    CredentialRefStore,
    CredentialResolver,
    ResolvedCredentials,
)
from mcp_multiplex.health import healthy_payload
from mcp_multiplex.observability import EventStore
from mcp_multiplex.runtime import (
    BackendSession,
    FrontendSession,
    HttpBackendRegistry,
    RuntimeBackendStore,
    RuntimeFrontendSessionStore,
    RuntimeProxyError,
    StdioBackendProcess,
    StdioBackendRegistry,
    restore_response_id,
    rewrite_request_id,
    runtime_pool_key,
)
from mcp_multiplex.schemas import CatalogEntry
from mcp_multiplex.security import SecurityError, validate_request_origin
from mcp_multiplex.storage import connect

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 30000
MCP_SESSION_HEADER = "Mcp-Session-Id"
MCP_PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"
AUTHORIZATION_HEADER = "Authorization"
SERVER_MCP_PATTERN = re.compile(r"^/servers/([A-Za-z0-9_.-]+)/mcp$")
CANCELLATION_METHODS = {"notifications/cancelled", "$/cancelRequest"}
CONTROL_MCP_PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
DEFAULT_CONTROL_MCP_PROTOCOL_VERSION = "2025-06-18"
LATEST_CONTROL_MCP_PROTOCOL_VERSION = CONTROL_MCP_PROTOCOL_VERSIONS[-1]


class MCPMultiplexHTTPServer(ThreadingHTTPServer):
    """Daemon HTTP server with optional local state database."""

    connection: sqlite3.Connection | None
    database_lock: threading.RLock
    http_backends: HttpBackendRegistry
    stdio_backends: StdioBackendRegistry

    def reap_idle_backends(self, *, now: datetime | None = None) -> list[str]:
        """Close idle managed backends with no attached frontend sessions."""
        if self.connection is None:
            return []
        return _reap_idle_backends(
            self.connection,
            self.database_lock,
            self.stdio_backends,
            self.http_backends,
            now=now,
        )

    def server_close(self) -> None:
        """Close managed backend processes before closing the listener."""
        if hasattr(self, "http_backends"):
            self.http_backends.close_all()
        if hasattr(self, "stdio_backends"):
            self.stdio_backends.close_all()
        super().server_close()


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for daemon foundation endpoints."""

    server_version = "MCPMultiplexDaemon/0.1"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(HTTPStatus.OK, healthy_payload())
            return
        if SERVER_MCP_PATTERN.match(self.path):
            self._send_json_rpc_error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                None,
                -32600,
                "Streamable HTTP MCP endpoint requires POST or DELETE",
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        match = SERVER_MCP_PATTERN.match(self.path)
        if match is None:
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        if not self._origin_allowed(json_rpc=True):
            return
        server_name = match.group(1)
        request_payload = self._read_json_body()
        if request_payload is None:
            return
        request_id = request_payload.get("id") if isinstance(request_payload, dict) else None
        if not isinstance(request_payload, dict) or request_payload.get("jsonrpc") != "2.0":
            self._send_json_rpc_error(
                HTTPStatus.BAD_REQUEST,
                request_id,
                -32600,
                "invalid JSON-RPC request",
            )
            return
        connection = self._connection()
        if connection is None:
            self._send_json_rpc_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                request_id,
                -32000,
                "runtime state database is not configured",
            )
            return
        if server_name == "mcp_hub":
            with self._database_lock():
                self._handle_control_mcp(connection, request_payload)
            return
        database_lock = self._database_lock()
        with database_lock:
            catalog_entry = _catalog_entry_for_server(connection, server_name)
        if catalog_entry is None:
            self._send_json_rpc_error(
                HTTPStatus.NOT_FOUND,
                request_id,
                -32004,
                f"unknown MCP server: {server_name}",
            )
            return

        method = request_payload.get("method")
        if method == "initialize":
            with database_lock:
                self._handle_initialize(connection, catalog_entry, request_payload)
            return

        session_id = self.headers.get(MCP_SESSION_HEADER)
        if not session_id:
            self._send_json_rpc_error(
                HTTPStatus.BAD_REQUEST,
                request_id,
                -32001,
                "missing Mcp-Session-Id header",
            )
            return
        try:
            with database_lock:
                session = RuntimeFrontendSessionStore(connection).touch(session_id)
        except KeyError:
            self._send_json_rpc_error(
                HTTPStatus.NOT_FOUND,
                request_id,
                -32001,
                "unknown frontend session",
            )
            return
        if not self._session_protocol_version_allowed(session, request_id):
            return
        if session.backend_id is None:
            self._send_json_rpc_error(
                HTTPStatus.NOT_IMPLEMENTED,
                request_id,
                -32002,
                "frontend session is not attached to a backend",
            )
            return
        try:
            with database_lock:
                backend_record = RuntimeBackendStore(connection).show(session.backend_id)
                backend_catalog_entry = CatalogStore(connection).show(backend_record.catalog_id)
        except KeyError:
            self._send_json_rpc_error(
                HTTPStatus.NOT_FOUND,
                request_id,
                -32001,
                "unknown backend session",
            )
            return
        method_name = str(method) if isinstance(method, str) else ""
        if method_name in CANCELLATION_METHODS:
            self._handle_cancellation(connection, backend_catalog_entry, session, request_payload)
            return
        if method_name.startswith("notifications/"):
            try:
                self._notify_backend(backend_catalog_entry, session.backend_id, request_payload)
                with database_lock:
                    RuntimeBackendStore(connection).touch(session.backend_id)
            except RuntimeProxyError as error:
                self._send_json_rpc_error(
                    HTTPStatus.BAD_GATEWAY,
                    request_id,
                    -32003,
                    str(error),
                )
                return
            self._send_empty(HTTPStatus.ACCEPTED)
            return
        try:
            if backend_record.state == "crashed":
                with database_lock:
                    backend_record = self._restart_backend(
                        connection, backend_catalog_entry, session, backend_record
                    )
            response_payload = self._forward_to_backend(
                backend_catalog_entry,
                session.frontend_session_id,
                session.backend_id,
                request_payload,
            )
            with database_lock:
                RuntimeBackendStore(connection).touch(session.backend_id)
        except (CredentialError, RuntimeProxyError) as error:
            with database_lock, suppress(KeyError):
                RuntimeBackendStore(connection).mark_crashed(session.backend_id)
                _emit_runtime_event(
                    connection,
                    "runtime.backend_crashed",
                    "failed",
                    {
                        "backend_id": session.backend_id,
                        "server_name": session.server_name,
                        "error": str(error),
                    },
                    agent_id=session.agent_id,
                )
            self._send_json_rpc_error(
                HTTPStatus.BAD_GATEWAY,
                request_id,
                -32003,
                str(error),
            )
            return
        self._send_json(HTTPStatus.OK, response_payload)

    def do_DELETE(self) -> None:
        match = SERVER_MCP_PATTERN.match(self.path)
        if match is None:
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        if not self._origin_allowed(json_rpc=False):
            return
        connection = self._connection()
        if connection is None:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "runtime state unavailable"})
            return
        session_id = self.headers.get(MCP_SESSION_HEADER)
        if not session_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing Mcp-Session-Id header"})
            return
        try:
            with self._database_lock():
                session = RuntimeFrontendSessionStore(connection).show(session_id)
                if not self._session_protocol_version_allowed(session, None):
                    return
                backend_catalog_entry: CatalogEntry | None = None
                if session.backend_id is not None:
                    backend_record = RuntimeBackendStore(connection).show(session.backend_id)
                    backend_catalog_entry = CatalogStore(connection).show(backend_record.catalog_id)
                RuntimeFrontendSessionStore(connection).delete(session_id)
                if session.backend_id is not None and backend_catalog_entry is not None:
                    backend_after_detach = RuntimeBackendStore(
                        connection
                    ).decrement_frontend_session_count(session.backend_id)
                    if backend_after_detach.frontend_session_count == 0:
                        self._close_backend(backend_catalog_entry, session.backend_id)
                        with suppress(KeyError):
                            RuntimeBackendStore(connection).mark_stopped(session.backend_id)
        except KeyError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown frontend session"})
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "frontend_session_id": session_id})

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr request logs for clean CLI operation."""

    def _handle_initialize(
        self,
        connection: sqlite3.Connection,
        catalog_entry: CatalogEntry,
        request_payload: dict[str, Any],
    ) -> None:
        params = request_payload.get("params")
        params_obj = params if isinstance(params, dict) else {}
        protocol_version = params_obj.get("protocolVersion")
        if protocol_version is not None and not isinstance(protocol_version, str):
            self._send_json_rpc_error(
                HTTPStatus.BAD_REQUEST,
                request_payload.get("id"),
                -32602,
                "params.protocolVersion must be a string",
            )
            return
        backend = catalog_entry.transport.backend
        if catalog_entry.runtime.shareability == "no_proxy":
            self._send_json_rpc_error(
                HTTPStatus.FORBIDDEN,
                request_payload.get("id"),
                -32005,
                "catalog policy forbids proxying this backend",
            )
            return
        if backend.type == "stdio":
            self._initialize_stdio_backend(connection, catalog_entry, request_payload)
            return
        if backend.type in {"streamable_http", "http"}:
            self._initialize_http_backend(connection, catalog_entry, request_payload)
            return
        self._send_json_rpc_error(
            HTTPStatus.NOT_IMPLEMENTED,
            request_payload.get("id"),
            -32002,
            "backend transport is not implemented",
        )

    def _handle_control_mcp(
        self,
        connection: sqlite3.Connection,
        request_payload: dict[str, Any],
    ) -> None:
        method = request_payload.get("method")
        request_id = request_payload.get("id")
        if method == "initialize":
            self._initialize_control_mcp(connection, request_payload)
            return

        session_id = self.headers.get(MCP_SESSION_HEADER)
        if not session_id:
            self._send_json_rpc_error(
                HTTPStatus.BAD_REQUEST,
                request_id,
                -32001,
                "missing Mcp-Session-Id header",
            )
            return
        try:
            session = RuntimeFrontendSessionStore(connection).touch(session_id)
        except KeyError:
            self._send_json_rpc_error(
                HTTPStatus.NOT_FOUND,
                request_id,
                -32001,
                "unknown frontend session",
            )
            return
        if session.server_name != "mcp_hub":
            self._send_json_rpc_error(
                HTTPStatus.FORBIDDEN,
                request_id,
                -32007,
                "frontend session is not scoped to mcp_hub",
            )
            return
        if not self._session_protocol_version_allowed(session, request_id):
            return
        if isinstance(method, str) and method.startswith("notifications/"):
            self._send_empty(HTTPStatus.ACCEPTED)
            return

        try:
            auth_token = _bearer_token(self.headers.get(AUTHORIZATION_HEADER))
            AuthTokenStore(connection).verify_local_token(auth_token, required_scope=CONTROL_READ)
        except AuthError as error:
            self._send_json_rpc_error(
                HTTPStatus.UNAUTHORIZED,
                request_id,
                -32007,
                str(error),
            )
            return

        from mcp_multiplex.control_mcp import ControlMCPServer

        control_server = ControlMCPServer(connection)
        if method == "tools/list":
            self._send_json(
                HTTPStatus.OK,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": control_server.list_tools()},
                },
            )
            return
        if method == "tools/call":
            self._handle_control_mcp_tool_call(control_server, request_payload, auth_token)
            return
        self._send_json_rpc_error(
            HTTPStatus.NOT_FOUND,
            request_id,
            -32601,
            f"unsupported mcp_hub method: {method}",
        )

    def _initialize_control_mcp(
        self,
        connection: sqlite3.Connection,
        request_payload: dict[str, Any],
    ) -> None:
        params = request_payload.get("params")
        params_obj = params if isinstance(params, dict) else {}
        protocol_version = params_obj.get("protocolVersion")
        if protocol_version is not None and not isinstance(protocol_version, str):
            self._send_json_rpc_error(
                HTTPStatus.BAD_REQUEST,
                request_payload.get("id"),
                -32602,
                "params.protocolVersion must be a string",
            )
            return
        negotiated_protocol_version = (
            protocol_version
            if protocol_version in CONTROL_MCP_PROTOCOL_VERSIONS
            else (
                DEFAULT_CONTROL_MCP_PROTOCOL_VERSION
                if protocol_version is None
                else LATEST_CONTROL_MCP_PROTOCOL_VERSION
            )
        )
        session = RuntimeFrontendSessionStore(connection).create(
            server_name="mcp_hub",
            agent_id=self.headers.get("X-MCP-Multiplex-Agent-ID"),
            workspace_root=self.headers.get("X-MCP-Multiplex-Workspace-Root"),
            protocol_version=negotiated_protocol_version,
        )
        self._send_json(
            HTTPStatus.OK,
            {
                "jsonrpc": "2.0",
                "id": request_payload.get("id"),
                "result": {
                    "capabilities": {"tools": {}},
                    "protocolVersion": negotiated_protocol_version,
                    "serverInfo": {"name": "mcp_hub", "version": "0.1.0"},
                },
            },
            headers={MCP_SESSION_HEADER: session.frontend_session_id},
        )

    def _session_protocol_version_allowed(
        self,
        session: FrontendSession,
        request_id: object,
    ) -> bool:
        supplied = self.headers.get(MCP_PROTOCOL_VERSION_HEADER)
        negotiated = session.protocol_version
        if supplied is None or negotiated is None or supplied == negotiated:
            return True
        self._send_json_rpc_error(
            HTTPStatus.BAD_REQUEST,
            request_id,
            -32600,
            (
                f"{MCP_PROTOCOL_VERSION_HEADER} does not match the initialized "
                f"session protocol version {negotiated}"
            ),
        )
        return False

    def _handle_control_mcp_tool_call(
        self,
        control_server: Any,
        request_payload: dict[str, Any],
        auth_token: str,
    ) -> None:
        params = request_payload.get("params")
        if not isinstance(params, dict):
            self._send_json_rpc_error(
                HTTPStatus.BAD_REQUEST,
                request_payload.get("id"),
                -32602,
                "params must be an object",
            )
            return
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            self._send_json_rpc_error(
                HTTPStatus.BAD_REQUEST,
                request_payload.get("id"),
                -32602,
                "params.name is required",
            )
            return
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            self._send_json_rpc_error(
                HTTPStatus.BAD_REQUEST,
                request_payload.get("id"),
                -32602,
                "params.arguments must be an object",
            )
            return
        try:
            result = control_server.call_tool(tool_name, arguments, auth_token=auth_token)
        except ValueError as error:
            self._send_json(
                HTTPStatus.OK,
                {
                    "jsonrpc": "2.0",
                    "id": request_payload.get("id"),
                    "result": {
                        "content": [{"type": "text", "text": str(error)}],
                        "isError": True,
                    },
                },
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "jsonrpc": "2.0",
                "id": request_payload.get("id"),
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, sort_keys=True, separators=(",", ":")),
                        }
                    ],
                    "isError": False,
                },
            },
        )

    def _initialize_stdio_backend(
        self,
        connection: sqlite3.Connection,
        catalog_entry: CatalogEntry,
        request_payload: dict[str, Any],
    ) -> None:
        backend = catalog_entry.transport.backend
        params = request_payload.get("params")
        params_obj = params if isinstance(params, dict) else {}
        protocol_version = params_obj.get("protocolVersion")
        if protocol_version is not None and not isinstance(protocol_version, str):
            self._send_json_rpc_error(
                HTTPStatus.BAD_REQUEST,
                request_payload.get("id"),
                -32602,
                "params.protocolVersion must be a string",
            )
            return
        session = RuntimeFrontendSessionStore(connection).create(
            server_name=catalog_entry.name,
            agent_id=self.headers.get("X-MCP-Multiplex-Agent-ID"),
            workspace_root=self.headers.get("X-MCP-Multiplex-Workspace-Root"),
            protocol_version=protocol_version,
        )
        if backend.command is None:
            self._send_json_rpc_error(
                HTTPStatus.BAD_GATEWAY,
                request_payload.get("id"),
                -32003,
                "stdio backend command is missing",
            )
            return
        pool_key = _runtime_pool_key(catalog_entry, session)
        reused = self._reuse_backend(
            connection, session.frontend_session_id, pool_key, request_payload
        )
        if reused is not None:
            reused_session = RuntimeFrontendSessionStore(connection).show(
                session.frontend_session_id
            )
            _emit_runtime_event(
                connection,
                "runtime.backend_reused",
                "success",
                {
                    "backend_id": reused_session.backend_id,
                    "server_name": catalog_entry.name,
                    "runtime_pool_key": pool_key,
                },
                agent_id=session.agent_id,
            )
            self._send_json(
                HTTPStatus.OK,
                reused,
                headers={MCP_SESSION_HEADER: session.frontend_session_id},
            )
            return
        process: StdioBackendProcess | None = None
        backend_id: str | None = None
        try:
            resolved_credentials = _resolve_backend_credentials(connection, catalog_entry)
            process = StdioBackendProcess.start(
                command=backend.command,
                args=backend.args,
                cwd=None,
                env=_stdio_start_env(resolved_credentials),
            )
            backend_record = RuntimeBackendStore(connection).create_starting(
                catalog_id=catalog_entry.catalog_id,
                runtime_pool_key=pool_key,
                pid=process.pid,
                workspace_root=session.workspace_root,
            )
            backend_id = backend_record.backend_id
            self._stdio_backends().register(backend_id, process)
            backend_payload, rewrite = rewrite_request_id(
                request_payload,
                frontend_session_id=session.frontend_session_id,
                backend_id=backend_id,
            )
            payload = restore_response_id(process.request(backend_payload), rewrite)
            RuntimeBackendStore(connection).mark_hot(backend_id, initialize_result=payload)
            RuntimeFrontendSessionStore(connection).attach_backend(
                session.frontend_session_id, backend_id
            )
            _emit_runtime_event(
                connection,
                "runtime.backend_started",
                "success",
                {
                    "backend_id": backend_id,
                    "server_name": catalog_entry.name,
                    "transport": "stdio",
                    "runtime_pool_key": pool_key,
                    "pid": process.pid,
                    **resolved_credentials.to_event_payload(),
                },
                agent_id=session.agent_id,
            )
        except (CredentialError, OSError, RuntimeProxyError) as error:
            if backend_id is not None:
                with suppress(KeyError):
                    RuntimeBackendStore(connection).mark_crashed(backend_id)
                self._stdio_backends().close(backend_id)
            elif process is not None:
                process.close()
            self._send_json_rpc_error(
                HTTPStatus.BAD_GATEWAY,
                request_payload.get("id"),
                -32003,
                f"stdio backend initialization failed: {error}",
            )
            return
        self._send_json(
            HTTPStatus.OK, payload, headers={MCP_SESSION_HEADER: session.frontend_session_id}
        )

    def _initialize_http_backend(
        self,
        connection: sqlite3.Connection,
        catalog_entry: CatalogEntry,
        request_payload: dict[str, Any],
    ) -> None:
        backend = catalog_entry.transport.backend
        params = request_payload.get("params")
        params_obj = params if isinstance(params, dict) else {}
        protocol_version = params_obj.get("protocolVersion")
        if protocol_version is not None and not isinstance(protocol_version, str):
            self._send_json_rpc_error(
                HTTPStatus.BAD_REQUEST,
                request_payload.get("id"),
                -32602,
                "params.protocolVersion must be a string",
            )
            return
        if backend.url is None:
            self._send_json_rpc_error(
                HTTPStatus.BAD_GATEWAY,
                request_payload.get("id"),
                -32003,
                "HTTP backend URL is missing",
            )
            return
        session = RuntimeFrontendSessionStore(connection).create(
            server_name=catalog_entry.name,
            agent_id=self.headers.get("X-MCP-Multiplex-Agent-ID"),
            workspace_root=self.headers.get("X-MCP-Multiplex-Workspace-Root"),
            protocol_version=protocol_version,
        )
        pool_key = _runtime_pool_key(catalog_entry, session)
        reused = self._reuse_backend(
            connection, session.frontend_session_id, pool_key, request_payload
        )
        if reused is not None:
            reused_session = RuntimeFrontendSessionStore(connection).show(
                session.frontend_session_id
            )
            _emit_runtime_event(
                connection,
                "runtime.backend_reused",
                "success",
                {
                    "backend_id": reused_session.backend_id,
                    "server_name": catalog_entry.name,
                    "runtime_pool_key": pool_key,
                },
                agent_id=session.agent_id,
            )
            self._send_json(
                HTTPStatus.OK,
                reused,
                headers={MCP_SESSION_HEADER: session.frontend_session_id},
            )
            return
        backend_id: str | None = None
        try:
            backend_record = RuntimeBackendStore(connection).create_starting(
                catalog_id=catalog_entry.catalog_id,
                runtime_pool_key=pool_key,
                pid=None,
                workspace_root=session.workspace_root,
            )
            backend_id = backend_record.backend_id
            self._http_backends().register(backend_id, url=backend.url)
            backend_payload, rewrite = rewrite_request_id(
                request_payload,
                frontend_session_id=session.frontend_session_id,
                backend_id=backend_id,
            )
            payload = restore_response_id(
                self._http_backends().request(backend_id, backend_payload), rewrite
            )
            RuntimeBackendStore(connection).mark_hot(backend_id, initialize_result=payload)
            RuntimeFrontendSessionStore(connection).attach_backend(
                session.frontend_session_id, backend_id
            )
            _emit_runtime_event(
                connection,
                "runtime.backend_started",
                "success",
                {
                    "backend_id": backend_id,
                    "server_name": catalog_entry.name,
                    "transport": backend.type,
                    "runtime_pool_key": pool_key,
                    "pid": None,
                },
                agent_id=session.agent_id,
            )
        except RuntimeProxyError as error:
            if backend_id is not None:
                with suppress(KeyError):
                    RuntimeBackendStore(connection).mark_crashed(backend_id)
                self._http_backends().close(backend_id)
            self._send_json_rpc_error(
                HTTPStatus.BAD_GATEWAY,
                request_payload.get("id"),
                -32003,
                f"HTTP backend initialization failed: {error}",
            )
            return
        self._send_json(
            HTTPStatus.OK, payload, headers={MCP_SESSION_HEADER: session.frontend_session_id}
        )

    def _read_json_body(self) -> Any | None:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "0")
        except ValueError:
            self._send_json_rpc_error(
                HTTPStatus.BAD_REQUEST, None, -32700, "invalid Content-Length"
            )
            return None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_json_rpc_error(HTTPStatus.BAD_REQUEST, None, -32700, "invalid JSON body")
            return None

    def _origin_allowed(self, *, json_rpc: bool) -> bool:
        try:
            validate_request_origin(self.headers.get("Origin"))
        except SecurityError as error:
            if json_rpc:
                self._send_json_rpc_error(
                    HTTPStatus.FORBIDDEN,
                    None,
                    -32006,
                    str(error),
                )
            else:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
            return False
        return True

    def _connection(self) -> sqlite3.Connection | None:
        server = self.server
        if not isinstance(server, MCPMultiplexHTTPServer):
            return None
        return server.connection

    def _database_lock(self) -> threading.RLock:
        server = self.server
        if not isinstance(server, MCPMultiplexHTTPServer):
            raise RuntimeError("unexpected HTTP server type")
        return server.database_lock

    def _stdio_backends(self) -> StdioBackendRegistry:
        server = self.server
        if not isinstance(server, MCPMultiplexHTTPServer):
            raise RuntimeError("unexpected HTTP server type")
        return server.stdio_backends

    def _http_backends(self) -> HttpBackendRegistry:
        server = self.server
        if not isinstance(server, MCPMultiplexHTTPServer):
            raise RuntimeError("unexpected HTTP server type")
        return server.http_backends

    def _require_local_auth(self, required_scope: str | None = None) -> AuthContext | None:
        connection = self._connection()
        if connection is None:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "auth state unavailable"})
            return None
        try:
            return require_local_auth(
                connection,
                self.headers.get(AUTHORIZATION_HEADER),
                required_scope=required_scope,
            )
        except AuthError as error:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": str(error)})
            return None

    def _forward_to_backend(
        self,
        catalog_entry: CatalogEntry,
        frontend_session_id: str,
        backend_id: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        backend = catalog_entry.transport.backend
        backend_payload, rewrite = rewrite_request_id(
            request_payload,
            frontend_session_id=frontend_session_id,
            backend_id=backend_id,
        )
        if backend.type == "stdio":
            response_payload = self._stdio_backends().request(backend_id, backend_payload)
            return restore_response_id(response_payload, rewrite)
        if backend.type in {"streamable_http", "http"}:
            response_payload = self._http_backends().request(backend_id, backend_payload)
            return restore_response_id(response_payload, rewrite)
        raise RuntimeProxyError("backend transport is not implemented")

    def _notify_backend(
        self,
        catalog_entry: CatalogEntry,
        backend_id: str,
        payload: dict[str, Any],
    ) -> None:
        backend = catalog_entry.transport.backend
        if backend.type == "stdio":
            self._stdio_backends().notify(backend_id, payload)
            return
        if backend.type in {"streamable_http", "http"}:
            self._http_backends().notify(backend_id, payload)
            return
        raise RuntimeProxyError("backend transport is not implemented")

    def _handle_cancellation(
        self,
        connection: sqlite3.Connection,
        catalog_entry: CatalogEntry,
        session: FrontendSession,
        request_payload: dict[str, Any],
    ) -> None:
        params = request_payload.get("params") if isinstance(request_payload, dict) else {}
        if session.backend_id is not None:
            with suppress(RuntimeProxyError):
                self._notify_backend(catalog_entry, session.backend_id, request_payload)
        _emit_runtime_event(
            connection,
            "runtime.request_cancelled",
            "success",
            {
                "frontend_session_id": session.frontend_session_id,
                "backend_id": session.backend_id,
                "server_name": session.server_name,
                "params": params,
            },
            agent_id=session.agent_id,
        )
        self._send_empty(HTTPStatus.ACCEPTED)

    def _restart_backend(
        self,
        connection: sqlite3.Connection,
        catalog_entry: CatalogEntry,
        session: FrontendSession,
        backend_record: BackendSession,
    ) -> BackendSession:
        backend = catalog_entry.transport.backend
        initialize_payload = {
            "jsonrpc": "2.0",
            "id": "runtime-restart-initialize",
            "method": "initialize",
            "params": {"protocolVersion": session.protocol_version or "2025-06-18"},
        }
        if backend.type == "stdio":
            if backend.command is None:
                raise RuntimeProxyError("stdio backend command is missing")
            resolved_credentials = _resolve_backend_credentials(connection, catalog_entry)
            process = StdioBackendProcess.start(
                command=backend.command,
                args=backend.args,
                cwd=None,
                env=_stdio_start_env(resolved_credentials),
            )
            RuntimeBackendStore(connection).mark_starting(
                backend_record.backend_id,
                pid=process.pid,
                frontend_session_count=max(backend_record.frontend_session_count, 1),
            )
            self._stdio_backends().register(backend_record.backend_id, process)
            backend_payload, rewrite = rewrite_request_id(
                initialize_payload,
                frontend_session_id=session.frontend_session_id,
                backend_id=backend_record.backend_id,
            )
            payload = restore_response_id(process.request(backend_payload), rewrite)
            restarted = RuntimeBackendStore(connection).mark_hot(
                backend_record.backend_id,
                initialize_result=payload,
                backend_initialize_count=backend_record.backend_initialize_count + 1,
            )
            _emit_runtime_event(
                connection,
                "runtime.backend_restarted",
                "success",
                {
                    "backend_id": backend_record.backend_id,
                    "server_name": catalog_entry.name,
                    "transport": "stdio",
                    "runtime_pool_key": backend_record.runtime_pool_key,
                    "pid": process.pid,
                    **resolved_credentials.to_event_payload(),
                },
                agent_id=session.agent_id,
            )
            return restarted
        if backend.type in {"streamable_http", "http"}:
            if backend.url is None:
                raise RuntimeProxyError("HTTP backend URL is missing")
            RuntimeBackendStore(connection).mark_starting(
                backend_record.backend_id,
                pid=None,
                frontend_session_count=max(backend_record.frontend_session_count, 1),
            )
            self._http_backends().register(backend_record.backend_id, url=backend.url)
            backend_payload, rewrite = rewrite_request_id(
                initialize_payload,
                frontend_session_id=session.frontend_session_id,
                backend_id=backend_record.backend_id,
            )
            payload = restore_response_id(
                self._http_backends().request(backend_record.backend_id, backend_payload), rewrite
            )
            restarted = RuntimeBackendStore(connection).mark_hot(
                backend_record.backend_id,
                initialize_result=payload,
                backend_initialize_count=backend_record.backend_initialize_count + 1,
            )
            _emit_runtime_event(
                connection,
                "runtime.backend_restarted",
                "success",
                {
                    "backend_id": backend_record.backend_id,
                    "server_name": catalog_entry.name,
                    "transport": backend.type,
                    "runtime_pool_key": backend_record.runtime_pool_key,
                    "pid": None,
                },
                agent_id=session.agent_id,
            )
            return restarted
        raise RuntimeProxyError("backend transport is not implemented")

    def _reuse_backend(
        self,
        connection: sqlite3.Connection,
        frontend_session_id: str,
        pool_key: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT catalog_id
            FROM runtime_backends
            WHERE runtime_pool_key = ?
              AND state = 'hot'
            LIMIT 1
            """,
            (pool_key,),
        ).fetchone()
        if row is None:
            return None
        backend = RuntimeBackendStore(connection).find_hot_by_pool(
            catalog_id=str(row["catalog_id"]), runtime_pool_key=pool_key
        )
        if backend is None or backend.initialize_result_json is None:
            return None
        RuntimeBackendStore(connection).increment_frontend_session_count(backend.backend_id)
        RuntimeFrontendSessionStore(connection).attach_backend(
            frontend_session_id, backend.backend_id
        )
        payload = json.loads(backend.initialize_result_json)
        if not isinstance(payload, dict):
            raise RuntimeProxyError("stored initialize result is invalid")
        if "id" in request_payload:
            payload = dict(payload)
            payload["id"] = request_payload["id"]
        return payload

    def _close_backend(self, catalog_entry: CatalogEntry, backend_id: str) -> None:
        backend = catalog_entry.transport.backend
        if backend.type == "stdio":
            self._stdio_backends().close(backend_id)
            return
        if backend.type in {"streamable_http", "http"}:
            self._http_backends().close(backend_id)

    def _send_json_rpc_error(
        self,
        status: HTTPStatus,
        request_id: object,
        code: int,
        message: str,
    ) -> None:
        self._send_json(
            status,
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        )

    def _send_json(
        self,
        status: HTTPStatus,
        payload: object,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


def build_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    connection: sqlite3.Connection | None = None,
) -> ThreadingHTTPServer:
    """Create the daemon HTTP server."""
    server = MCPMultiplexHTTPServer((host, port), HealthHandler)
    server.connection = connection
    server.database_lock = threading.RLock()
    server.http_backends = HttpBackendRegistry()
    server.stdio_backends = StdioBackendRegistry()
    return server


def _catalog_entry_for_server(
    connection: sqlite3.Connection, server_name: str
) -> CatalogEntry | None:
    row = connection.execute(
        """
        SELECT catalog_id
        FROM catalog_entries
        WHERE name = ?
          AND review_state = 'approved'
          AND lifecycle_state = 'enabled'
        LIMIT 1
        """,
        (server_name,),
    ).fetchone()
    if row is None:
        return None
    return CatalogStore(connection).show(str(row["catalog_id"]))


def _runtime_pool_key(catalog_entry: CatalogEntry, session: object) -> str:
    if not hasattr(session, "frontend_session_id"):
        raise RuntimeProxyError("frontend session is missing an id")
    return runtime_pool_key(
        catalog_id=catalog_entry.catalog_id,
        shareability=catalog_entry.runtime.shareability,
        frontend_session_id=str(session.frontend_session_id),
        workspace_root=getattr(session, "workspace_root", None),
        agent_id=getattr(session, "agent_id", None),
        remote_url=catalog_entry.transport.backend.url,
        transport_type=catalog_entry.transport.backend.type,
    )


def _resolve_backend_credentials(
    connection: sqlite3.Connection,
    catalog_entry: CatalogEntry,
    *,
    resolver: CredentialResolver | None = None,
) -> ResolvedCredentials:
    required_env_names = catalog_entry.transport.backend.env
    if not required_env_names:
        return ResolvedCredentials(env={}, resolved_names=[])
    return CredentialRefStore(connection).resolve_for_backend_startup(
        catalog_id=catalog_entry.catalog_id,
        required_env_names=required_env_names,
        resolver=resolver,
    )


def _stdio_start_env(resolved_credentials: ResolvedCredentials) -> dict[str, str]:
    """Build a child process env without inheriting arbitrary secret-bearing variables."""
    env: dict[str, str] = {}
    for name in ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "COMSPEC"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env.update(resolved_credentials.env)
    return env


def require_local_auth(
    connection: sqlite3.Connection,
    authorization_header: str | None,
    *,
    required_scope: str | None = None,
) -> AuthContext:
    """Validate a local Bearer token for control-plane/API endpoints."""
    token = _bearer_token(authorization_header)
    return AuthTokenStore(connection).verify_local_token(token, required_scope=required_scope)


def _bearer_token(authorization_header: str | None) -> str:
    if not authorization_header:
        raise AuthError("missing Authorization bearer token")
    scheme, separator, token = authorization_header.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        raise AuthError("invalid Authorization bearer token")
    return token


def _reap_idle_backends(
    connection: sqlite3.Connection,
    database_lock: threading.RLock,
    stdio_backends: StdioBackendRegistry,
    http_backends: HttpBackendRegistry,
    *,
    now: datetime | None = None,
) -> list[str]:
    reaped: list[str] = []
    current_time = now or datetime.now(UTC)
    with database_lock:
        backends = RuntimeBackendStore(connection).list()
        catalog_store = CatalogStore(connection)
        for backend in backends:
            if backend.state != "hot" or backend.frontend_session_count > 0:
                continue
            catalog_entry = catalog_store.show(backend.catalog_id)
            last_used_at = _parse_timestamp(backend.last_used_at or backend.created_at)
            age_seconds = (current_time - last_used_at).total_seconds()
            if age_seconds < catalog_entry.runtime.idle_timeout_sec:
                continue
            if catalog_entry.transport.backend.type == "stdio":
                stdio_backends.close(backend.backend_id)
            elif catalog_entry.transport.backend.type in {"streamable_http", "http"}:
                http_backends.close(backend.backend_id)
            RuntimeBackendStore(connection).mark_stopped(backend.backend_id)
            _emit_runtime_event(
                connection,
                "runtime.backend_reaped",
                "success",
                {
                    "backend_id": backend.backend_id,
                    "server_name": catalog_entry.name,
                    "runtime_pool_key": backend.runtime_pool_key,
                    "idle_timeout_sec": catalog_entry.runtime.idle_timeout_sec,
                },
            )
            reaped.append(backend.backend_id)
    return reaped


def _emit_runtime_event(
    connection: sqlite3.Connection,
    event_type: str,
    result: str,
    payload: dict[str, Any],
    *,
    agent_id: str | None = None,
) -> None:
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    hash_payload = {
        "event_type": event_type,
        "payload": payload,
        "timestamp": timestamp,
        "nonce": uuid.uuid4().hex,
    }
    digest = hashlib.sha256(
        json.dumps(
            hash_payload,
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()[:24]
    EventStore(connection).append(
        event_id=f"evt_{digest}",
        event_type=event_type,
        actor="daemon",
        agent_id=agent_id,
        result=result,
        payload=payload,
        timestamp=timestamp,
    )


def _parse_timestamp(value: str) -> datetime:
    timestamp = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the daemon until interrupted."""
    parser = argparse.ArgumentParser(prog="mcp-multiplex-daemon")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    parser.add_argument("--db-path", default=None)
    args = parser.parse_args(argv)

    connection = connect(args.db_path) if args.db_path else None
    server = build_server(args.host, args.port, connection=connection)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0
