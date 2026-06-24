"""Runtime proxy session storage and local backend process management."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp_multiplex.storage import migrate

MCP_SESSION_HEADER = "Mcp-Session-Id"


class RuntimeError(ValueError):
    """Raised for invalid runtime proxy state."""


class RuntimeProxyError(ValueError):
    """Raised when a managed backend cannot serve a proxy request."""


@dataclass(frozen=True)
class FrontendSession:
    """One client-facing MCP frontend session."""

    frontend_session_id: str
    backend_id: str | None
    agent_id: str | None
    server_name: str
    workspace_root: str | None
    protocol_version: str | None
    created_at: str
    last_seen_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "frontend_session_id": self.frontend_session_id,
            "backend_id": self.backend_id,
            "agent_id": self.agent_id,
            "server_name": self.server_name,
            "workspace_root": self.workspace_root,
            "protocol_version": self.protocol_version,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
        }


@dataclass(frozen=True)
class BackendSession:
    """One managed backend process or remote runtime session."""

    backend_id: str
    catalog_id: str
    runtime_pool_key: str
    state: str
    pid: int | None
    account_scope: str | None
    workspace_root: str | None
    backend_initialize_count: int
    frontend_session_count: int
    initialize_result_json: str | None
    created_at: str
    last_used_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "catalog_id": self.catalog_id,
            "runtime_pool_key": self.runtime_pool_key,
            "state": self.state,
            "pid": self.pid,
            "account_scope": self.account_scope,
            "workspace_root": self.workspace_root,
            "backend_initialize_count": self.backend_initialize_count,
            "frontend_session_count": self.frontend_session_count,
            "initialize_result_json": self.initialize_result_json,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


class RuntimeFrontendSessionStore:
    """SQLite-backed frontend session repository."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate(connection)

    def create(
        self,
        *,
        server_name: str,
        agent_id: str | None = None,
        workspace_root: str | None = None,
        protocol_version: str | None = None,
        created_at: str | None = None,
        frontend_session_id: str | None = None,
    ) -> FrontendSession:
        """Create a frontend session without assigning a backend yet."""
        timestamp = created_at or _current_timestamp()
        session_id = frontend_session_id or new_frontend_session_id()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO runtime_frontend_sessions (
                  frontend_session_id,
                  backend_id,
                  agent_id,
                  server_name,
                  workspace_root,
                  protocol_version,
                  created_at,
                  last_seen_at
                )
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    agent_id,
                    server_name,
                    workspace_root,
                    protocol_version,
                    timestamp,
                    timestamp,
                ),
            )
        return self.show(session_id)

    def attach_backend(self, frontend_session_id: str, backend_id: str) -> FrontendSession:
        """Attach a frontend session to a managed backend."""
        timestamp = _current_timestamp()
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE runtime_frontend_sessions
                SET backend_id = ?, last_seen_at = ?
                WHERE frontend_session_id = ?
                """,
                (backend_id, timestamp, frontend_session_id),
            ).rowcount
        if updated == 0:
            raise KeyError(frontend_session_id)
        return self.show(frontend_session_id)

    def show(self, frontend_session_id: str) -> FrontendSession:
        """Return one frontend session."""
        row = self.connection.execute(
            """
            SELECT *
            FROM runtime_frontend_sessions
            WHERE frontend_session_id = ?
            """,
            (frontend_session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(frontend_session_id)
        return _session_from_row(row)

    def list(self, *, server_name: str | None = None) -> list[FrontendSession]:
        """List frontend sessions in deterministic order."""
        clauses: list[str] = []
        params: list[str] = []
        if server_name is not None:
            clauses.append("server_name = ?")
            params.append(server_name)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT frontend_session_id
            FROM runtime_frontend_sessions
            {where}
            ORDER BY created_at, frontend_session_id
            """,
            params,
        ).fetchall()
        return [self.show(str(row["frontend_session_id"])) for row in rows]

    def touch(
        self, frontend_session_id: str, *, last_seen_at: str | None = None
    ) -> FrontendSession:
        """Update session last-seen timestamp."""
        timestamp = last_seen_at or _current_timestamp()
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE runtime_frontend_sessions
                SET last_seen_at = ?
                WHERE frontend_session_id = ?
                """,
                (timestamp, frontend_session_id),
            ).rowcount
        if updated == 0:
            raise KeyError(frontend_session_id)
        return self.show(frontend_session_id)

    def delete(self, frontend_session_id: str) -> None:
        """Delete one frontend session."""
        with self.connection:
            deleted = self.connection.execute(
                "DELETE FROM runtime_frontend_sessions WHERE frontend_session_id = ?",
                (frontend_session_id,),
            ).rowcount
        if deleted == 0:
            raise KeyError(frontend_session_id)


class RuntimeBackendStore:
    """SQLite-backed backend session repository."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate(connection)

    def create_starting(
        self,
        *,
        catalog_id: str,
        runtime_pool_key: str,
        pid: int | None,
        workspace_root: str | None = None,
        account_scope: str | None = None,
        frontend_session_count: int = 1,
        backend_id: str | None = None,
    ) -> BackendSession:
        """Create a backend row while a process is being initialized."""
        session_id = backend_id or new_backend_id(catalog_id, runtime_pool_key)
        timestamp = _current_timestamp()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO runtime_backends (
                  backend_id,
                  catalog_id,
                  runtime_pool_key,
                  state,
                  pid,
                  account_scope,
                  workspace_root,
                  frontend_session_count,
                  created_at,
                  last_used_at
                )
                VALUES (?, ?, ?, 'starting', ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    catalog_id,
                    runtime_pool_key,
                    pid,
                    account_scope,
                    workspace_root,
                    frontend_session_count,
                    timestamp,
                    timestamp,
                ),
            )
        return self.show(session_id)

    def mark_hot(
        self,
        backend_id: str,
        *,
        initialize_result: dict[str, Any],
        backend_initialize_count: int = 1,
    ) -> BackendSession:
        """Mark a backend initialized and ready for requests."""
        timestamp = _current_timestamp()
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE runtime_backends
                SET state = 'hot',
                    backend_initialize_count = ?,
                    initialize_result_json = ?,
                    last_used_at = ?
                WHERE backend_id = ?
                """,
                (
                    backend_initialize_count,
                    json.dumps(initialize_result, sort_keys=True, separators=(",", ":")),
                    timestamp,
                    backend_id,
                ),
            ).rowcount
        if updated == 0:
            raise KeyError(backend_id)
        return self.show(backend_id)

    def mark_starting(
        self,
        backend_id: str,
        *,
        pid: int | None,
        frontend_session_count: int | None = None,
    ) -> BackendSession:
        """Mark an existing backend row as restarting."""
        timestamp = _current_timestamp()
        count_sql = (
            "frontend_session_count = ?,"
            if frontend_session_count is not None
            else "frontend_session_count = frontend_session_count,"
        )
        params: list[Any] = [pid]
        if frontend_session_count is not None:
            params.append(frontend_session_count)
        params.extend([timestamp, backend_id])
        with self.connection:
            updated = self.connection.execute(
                f"""
                UPDATE runtime_backends
                SET state = 'starting',
                    pid = ?,
                    {count_sql}
                    initialize_result_json = NULL,
                    last_used_at = ?
                WHERE backend_id = ?
                """,
                params,
            ).rowcount
        if updated == 0:
            raise KeyError(backend_id)
        return self.show(backend_id)

    def increment_frontend_session_count(self, backend_id: str) -> BackendSession:
        """Record one additional frontend session using a backend."""
        timestamp = _current_timestamp()
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE runtime_backends
                SET frontend_session_count = frontend_session_count + 1,
                    last_used_at = ?
                WHERE backend_id = ?
                """,
                (timestamp, backend_id),
            ).rowcount
        if updated == 0:
            raise KeyError(backend_id)
        return self.show(backend_id)

    def decrement_frontend_session_count(self, backend_id: str) -> BackendSession:
        """Record one frontend session no longer using a backend."""
        timestamp = _current_timestamp()
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE runtime_backends
                SET frontend_session_count = MAX(frontend_session_count - 1, 0),
                    last_used_at = ?
                WHERE backend_id = ?
                """,
                (timestamp, backend_id),
            ).rowcount
        if updated == 0:
            raise KeyError(backend_id)
        return self.show(backend_id)

    def mark_crashed(self, backend_id: str) -> BackendSession:
        """Mark a backend as crashed."""
        timestamp = _current_timestamp()
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE runtime_backends
                SET state = 'crashed', last_used_at = ?
                WHERE backend_id = ?
                """,
                (timestamp, backend_id),
            ).rowcount
        if updated == 0:
            raise KeyError(backend_id)
        return self.show(backend_id)

    def mark_stopped(self, backend_id: str) -> BackendSession:
        """Mark a backend as intentionally stopped."""
        timestamp = _current_timestamp()
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE runtime_backends
                SET state = 'stopped', last_used_at = ?
                WHERE backend_id = ?
                """,
                (timestamp, backend_id),
            ).rowcount
        if updated == 0:
            raise KeyError(backend_id)
        return self.show(backend_id)

    def touch(self, backend_id: str) -> BackendSession:
        """Update backend last-used timestamp."""
        timestamp = _current_timestamp()
        with self.connection:
            updated = self.connection.execute(
                "UPDATE runtime_backends SET last_used_at = ? WHERE backend_id = ?",
                (timestamp, backend_id),
            ).rowcount
        if updated == 0:
            raise KeyError(backend_id)
        return self.show(backend_id)

    def show(self, backend_id: str) -> BackendSession:
        """Return one backend session."""
        row = self.connection.execute(
            """
            SELECT *
            FROM runtime_backends
            WHERE backend_id = ?
            """,
            (backend_id,),
        ).fetchone()
        if row is None:
            raise KeyError(backend_id)
        return _backend_from_row(row)

    def find_hot_by_pool(self, *, catalog_id: str, runtime_pool_key: str) -> BackendSession | None:
        """Return a hot backend matching a catalog/pool pair."""
        row = self.connection.execute(
            """
            SELECT backend_id
            FROM runtime_backends
            WHERE catalog_id = ?
              AND runtime_pool_key = ?
              AND state = 'hot'
            LIMIT 1
            """,
            (catalog_id, runtime_pool_key),
        ).fetchone()
        if row is None:
            return None
        return self.show(str(row["backend_id"]))

    def list(self) -> list[BackendSession]:
        """List backend sessions in deterministic order."""
        rows = self.connection.execute(
            """
            SELECT backend_id
            FROM runtime_backends
            ORDER BY created_at, backend_id
            """
        ).fetchall()
        return [self.show(str(row["backend_id"])) for row in rows]


class StdioBackendProcess:
    """Line-delimited JSON-RPC transport for one local stdio MCP process."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        if process.stdin is None or process.stdout is None:
            raise RuntimeProxyError("stdio backend process is missing pipes")
        self.process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._lock = threading.Lock()

    @classmethod
    def start(
        cls,
        *,
        command: str,
        args: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> StdioBackendProcess:
        """Start a local stdio backend process."""
        argv = [command, *args]
        process = subprocess.Popen(
            argv,
            cwd=str(Path(cwd)) if cwd else None,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        return cls(process)

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one JSON-RPC request and read one JSON-RPC response."""
        with self._lock:
            if self.process.poll() is not None:
                raise RuntimeProxyError("stdio backend process is not running")
            try:
                self._stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
                self._stdin.flush()
                line = self._stdout.readline()
            except OSError as error:
                raise RuntimeProxyError("stdio backend transport failed") from error
        if not line:
            raise RuntimeProxyError("stdio backend closed without a response")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeProxyError("stdio backend returned invalid JSON") from error
        if not isinstance(response, dict):
            raise RuntimeProxyError("stdio backend returned a non-object JSON-RPC response")
        return response

    def notify(self, payload: dict[str, Any]) -> None:
        """Send one JSON-RPC notification without waiting for a response."""
        with self._lock:
            if self.process.poll() is not None:
                raise RuntimeProxyError("stdio backend process is not running")
            try:
                self._stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
                self._stdin.flush()
            except OSError as error:
                raise RuntimeProxyError("stdio backend notification failed") from error

    def close(self) -> None:
        """Terminate the backend process without killing unrelated processes."""
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)


class StdioBackendRegistry:
    """In-memory registry for stdio backend process handles."""

    def __init__(self) -> None:
        self._processes: dict[str, StdioBackendProcess] = {}
        self._lock = threading.Lock()

    def register(self, backend_id: str, process: StdioBackendProcess) -> None:
        """Register one started process."""
        with self._lock:
            existing = self._processes.pop(backend_id, None)
            if existing is not None:
                existing.close()
            self._processes[backend_id] = process

    def request(self, backend_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Forward one request to a registered backend."""
        with self._lock:
            process = self._processes.get(backend_id)
        if process is None:
            raise RuntimeProxyError("stdio backend process is not registered")
        return process.request(payload)

    def notify(self, backend_id: str, payload: dict[str, Any]) -> None:
        """Forward one notification to a registered backend."""
        with self._lock:
            process = self._processes.get(backend_id)
        if process is None:
            raise RuntimeProxyError("stdio backend process is not registered")
        process.notify(payload)

    def close(self, backend_id: str) -> None:
        """Close and remove one backend process."""
        with self._lock:
            process = self._processes.pop(backend_id, None)
        if process is not None:
            process.close()

    def close_all(self) -> None:
        """Close all registered backend processes."""
        with self._lock:
            processes = list(self._processes.values())
            self._processes.clear()
        for process in processes:
            process.close()


@dataclass(frozen=True)
class HttpBackendSession:
    """One remote Streamable HTTP backend endpoint/session pair."""

    url: str
    backend_session_id: str | None = None


@dataclass(frozen=True)
class RequestIdRewrite:
    """Synchronous mapping between a frontend JSON-RPC id and backend id."""

    frontend_id: object
    backend_id: str


class HttpBackendClient:
    """JSON-RPC client for one remote HTTP MCP backend."""

    def __init__(self, *, url: str, backend_session_id: str | None = None) -> None:
        self.url = url
        self.backend_session_id = backend_session_id
        self._lock = threading.Lock()

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Forward one JSON-RPC request to a remote HTTP MCP backend."""
        with self._lock:
            response_payload, backend_session_id = _post_json_rpc(
                self.url, payload, backend_session_id=self.backend_session_id
            )
            if backend_session_id:
                self.backend_session_id = backend_session_id
            return response_payload

    def notify(self, payload: dict[str, Any]) -> None:
        """Forward one notification to a remote HTTP MCP backend."""
        with self._lock:
            response_payload, backend_session_id = _post_json_rpc(
                self.url, payload, backend_session_id=self.backend_session_id
            )
            if response_payload:
                raise RuntimeProxyError("HTTP backend returned a response to a notification")
            if backend_session_id:
                self.backend_session_id = backend_session_id

    def close(self) -> None:
        """Best-effort remote session deletion for managed backend sessions."""
        if not self.backend_session_id:
            return
        request = Request(
            self.url,
            method="DELETE",
            headers={MCP_SESSION_HEADER: self.backend_session_id},
        )
        try:
            with urlopen(request, timeout=5):
                pass
        except (HTTPError, URLError, TimeoutError, OSError):
            return


class HttpBackendRegistry:
    """In-memory registry for remote HTTP backend session handles."""

    def __init__(self) -> None:
        self._clients: dict[str, HttpBackendClient] = {}
        self._lock = threading.Lock()

    def register(
        self,
        backend_id: str,
        *,
        url: str,
        backend_session_id: str | None = None,
    ) -> None:
        """Register one remote backend endpoint."""
        with self._lock:
            existing = self._clients.pop(backend_id, None)
            if existing is not None:
                existing.close()
            self._clients[backend_id] = HttpBackendClient(
                url=url, backend_session_id=backend_session_id
            )

    def request(self, backend_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Forward one request to a registered remote backend."""
        with self._lock:
            client = self._clients.get(backend_id)
        if client is None:
            raise RuntimeProxyError("HTTP backend session is not registered")
        return client.request(payload)

    def notify(self, backend_id: str, payload: dict[str, Any]) -> None:
        """Forward one notification to a registered remote backend."""
        with self._lock:
            client = self._clients.get(backend_id)
        if client is None:
            raise RuntimeProxyError("HTTP backend session is not registered")
        client.notify(payload)

    def close(self, backend_id: str) -> None:
        """Close and remove one remote backend session."""
        with self._lock:
            client = self._clients.pop(backend_id, None)
        if client is not None:
            client.close()

    def close_all(self) -> None:
        """Close all registered remote backend sessions."""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()


def new_frontend_session_id() -> str:
    """Return an opaque frontend session id."""
    digest = hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:24]
    return f"fs_{digest}"


def new_backend_id(catalog_id: str, runtime_pool_key: str) -> str:
    """Return a deterministic backend id for a catalog/pool pair."""
    digest = hashlib.sha256(f"{catalog_id}\0{runtime_pool_key}".encode()).hexdigest()[:24]
    return f"be_{digest}"


def isolated_pool_key(frontend_session_id: str) -> str:
    """Return the TASK-025 conservative per-frontend runtime pool key."""
    return f"isolated_frontend:{frontend_session_id}"


def runtime_pool_key(
    *,
    catalog_id: str,
    shareability: str,
    frontend_session_id: str,
    workspace_root: str | None = None,
    agent_id: str | None = None,
    account_scope: str | None = None,
    remote_url: str | None = None,
    transport_type: str | None = None,
) -> str:
    """Return the runtime pool key dictated by catalog shareability policy."""
    base = f"catalog:{catalog_id}"
    if shareability == "global":
        return f"global:{base}"
    if shareability == "per_workspace" and workspace_root:
        return f"workspace:{base}:{_stable_component(workspace_root)}"
    if shareability == "per_agent" and agent_id:
        return f"agent:{base}:{_stable_component(agent_id)}"
    if shareability == "per_account" and account_scope:
        return f"account:{base}:{_stable_component(account_scope)}"
    if shareability == "no_proxy":
        raise RuntimeProxyError("catalog policy forbids proxying this backend")
    identity = {
        "shareability": shareability,
        "frontend_session_id": frontend_session_id,
        "transport_type": transport_type,
        "remote_url": remote_url,
    }
    return (
        f"isolated:{base}:"
        f"{hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]}"
    )


def rewrite_request_id(
    payload: dict[str, Any], *, frontend_session_id: str, backend_id: str
) -> tuple[dict[str, Any], RequestIdRewrite | None]:
    """Rewrite one JSON-RPC request id before forwarding to a backend."""
    if "id" not in payload:
        return dict(payload), None
    frontend_id = payload["id"]
    backend_request_id = new_backend_request_id(frontend_session_id, backend_id, frontend_id)
    rewritten = dict(payload)
    rewritten["id"] = backend_request_id
    return rewritten, RequestIdRewrite(frontend_id=frontend_id, backend_id=backend_request_id)


def restore_response_id(
    payload: dict[str, Any], rewrite: RequestIdRewrite | None
) -> dict[str, Any]:
    """Restore a backend response id to the frontend id."""
    if rewrite is None or "id" not in payload:
        return dict(payload)
    if payload["id"] != rewrite.backend_id:
        raise RuntimeProxyError("backend response id did not match mapped request id")
    restored = dict(payload)
    restored["id"] = rewrite.frontend_id
    return restored


def new_backend_request_id(frontend_session_id: str, backend_id: str, frontend_id: object) -> str:
    """Return an opaque backend request id for one frontend request id."""
    nonce = uuid.uuid4().hex
    digest = hashlib.sha256(
        json.dumps(
            {
                "frontend_session_id": frontend_session_id,
                "backend_id": backend_id,
                "frontend_id": frontend_id,
                "nonce": nonce,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()[:24]
    return f"hb_{digest}"


def _post_json_rpc(
    url: str, payload: dict[str, Any], *, backend_session_id: str | None = None
) -> tuple[dict[str, Any], str | None]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if backend_session_id is not None:
        headers[MCP_SESSION_HEADER] = backend_session_id
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urlopen(request, timeout=10) as response:
            raw_body = response.read()
            response_session_id = response.headers.get(MCP_SESSION_HEADER)
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeProxyError(f"HTTP backend returned {error.code}: {body}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeProxyError(f"HTTP backend request failed: {error}") from error
    try:
        response_payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeProxyError("HTTP backend returned invalid JSON") from error
    if not isinstance(response_payload, dict):
        raise RuntimeProxyError("HTTP backend returned a non-object JSON-RPC response")
    return response_payload, response_session_id


def _stable_component(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _session_from_row(row: sqlite3.Row) -> FrontendSession:
    return FrontendSession(
        frontend_session_id=str(row["frontend_session_id"]),
        backend_id=row["backend_id"],
        agent_id=row["agent_id"],
        server_name=str(row["server_name"]),
        workspace_root=row["workspace_root"],
        protocol_version=row["protocol_version"],
        created_at=str(row["created_at"]),
        last_seen_at=row["last_seen_at"],
    )


def _backend_from_row(row: sqlite3.Row) -> BackendSession:
    return BackendSession(
        backend_id=str(row["backend_id"]),
        catalog_id=str(row["catalog_id"]),
        runtime_pool_key=str(row["runtime_pool_key"]),
        state=str(row["state"]),
        pid=row["pid"],
        account_scope=row["account_scope"],
        workspace_root=row["workspace_root"],
        backend_initialize_count=int(row["backend_initialize_count"]),
        frontend_session_count=int(row["frontend_session_count"]),
        initialize_result_json=row["initialize_result_json"],
        created_at=str(row["created_at"]),
        last_used_at=row["last_used_at"],
    )


def _current_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "BackendSession",
    "FrontendSession",
    "HttpBackendRegistry",
    "HttpBackendSession",
    "RuntimeError",
    "RuntimeBackendStore",
    "RuntimeFrontendSessionStore",
    "RuntimeProxyError",
    "RequestIdRewrite",
    "StdioBackendProcess",
    "StdioBackendRegistry",
    "isolated_pool_key",
    "new_backend_id",
    "new_backend_request_id",
    "new_frontend_session_id",
    "restore_response_id",
    "runtime_pool_key",
    "rewrite_request_id",
]
