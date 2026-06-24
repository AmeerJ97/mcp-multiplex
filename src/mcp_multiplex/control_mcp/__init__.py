"""Read-only `mcp_hub` control-plane MCP tools."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from mcp_multiplex.auth import CONTROL_READ, AuthContext, AuthError, AuthTokenStore
from mcp_multiplex.catalog import CatalogStore
from mcp_multiplex.cli import status_payload
from mcp_multiplex.credentials import CredentialRefStore, readiness_summary
from mcp_multiplex.observability import ObservedEntryStore
from mcp_multiplex.runtime import RuntimeBackendStore
from mcp_multiplex.storage import migrate

TOOL_NAMES = (
    "self_check",
    "status",
    "plan_list",
    "plan_show",
    "proxy_url",
    "runtime_status",
    "credential_status",
    "catalog_search",
)


class ControlMCPError(ValueError):
    """Raised when a control-plane MCP tool call is invalid or unauthorized."""


@dataclass(frozen=True)
class ControlMCPTool:
    """Tool metadata returned to MCP clients."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass(frozen=True)
class ControlMCPServer:
    """In-process dispatcher for the `mcp_hub` control-plane MCP."""

    connection: sqlite3.Connection

    def __post_init__(self) -> None:
        migrate(self.connection)

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the stable read-only MCP tool list."""
        return [tool.to_dict() for tool in _tool_metadata()]

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        auth_token: str | None,
    ) -> dict[str, Any]:
        """Call one read-only control-plane tool with an agent-scoped auth token."""
        context = self._auth_context(auth_token)
        args = arguments or {}
        if name == "self_check":
            return self.self_check(context)
        if name == "status":
            return self.status(context)
        if name == "plan_list":
            return self.plan_list(context, status=_optional_str(args, "status"))
        if name == "plan_show":
            return self.plan_show(context, plan_id=_required_str(args, "plan_id"))
        if name == "proxy_url":
            return self.proxy_url(context, server_name=_required_str(args, "server_name"))
        if name == "runtime_status":
            return self.runtime_status(context, server_name=_optional_str(args, "server_name"))
        if name == "credential_status":
            return self.credential_status(context)
        if name == "catalog_search":
            return self.catalog_search(context, query=_optional_str(args, "query"))
        raise ControlMCPError(f"unknown mcp_hub tool: {name}")

    def self_check(self, context: AuthContext) -> dict[str, Any]:
        """Return agent-scoped compliance state and remediation plan ids."""
        agent_id = _require_agent_context(context)
        status = self.status(context)
        plans = self.plan_list(context)
        return {
            "schema_version": 1,
            "kind": "MCPHubSelfCheck",
            "agent_id": agent_id,
            "ok": status["status"]["ok"],
            "status": status["status"],
            "plan_ids": [plan["plan_id"] for plan in plans["plans"]],
            "destructive_actions": [],
            "destructive_actions_require_approval": True,
        }

    def status(self, context: AuthContext) -> dict[str, Any]:
        """Return agent-scoped status."""
        agent_id = _require_agent_context(context)
        status = status_payload(self.connection)
        observed = ObservedEntryStore(self.connection).list(agent_id=agent_id)
        scoped = _scope_status_to_agent(status, agent_id)
        scoped["observed_entries"] = [entry.to_dict() for entry in observed]
        return {"schema_version": 1, "kind": "MCPHubStatus", "agent_id": agent_id, "status": scoped}

    def plan_list(self, context: AuthContext, *, status: str | None = None) -> dict[str, Any]:
        """Return agent-scoped remediation plans."""
        agent_id = _require_agent_context(context)
        plans = _plan_rows(self.connection, agent_id=agent_id, status=status)
        return {"schema_version": 1, "kind": "MCPHubPlanList", "agent_id": agent_id, "plans": plans}

    def plan_show(self, context: AuthContext, *, plan_id: str) -> dict[str, Any]:
        """Return one agent-scoped remediation plan."""
        agent_id = _require_agent_context(context)
        plan = _plan_row(self.connection, plan_id)
        if plan["agent_id"] != agent_id:
            raise ControlMCPError("plan is not scoped to invoking agent")
        return {"schema_version": 1, "kind": "MCPHubPlanShow", "agent_id": agent_id, "plan": plan}

    def proxy_url(self, context: AuthContext, *, server_name: str) -> dict[str, Any]:
        """Return the Hub-owned per-server URL."""
        agent_id = _require_agent_context(context)
        catalog_entry = _catalog_entry_by_name(self.connection, server_name)
        if catalog_entry is None:
            raise ControlMCPError(f"unknown server: {server_name}")
        return {
            "schema_version": 1,
            "kind": "MCPHubProxyURL",
            "agent_id": agent_id,
            "server_name": server_name,
            "catalog_id": catalog_entry.catalog_id,
            "url": f"http://127.0.0.1:30000{catalog_entry.transport.hub_path}",
        }

    def runtime_status(
        self, context: AuthContext, *, server_name: str | None = None
    ) -> dict[str, Any]:
        """Return runtime status, optionally filtered by server name."""
        agent_id = _require_agent_context(context)
        catalog_ids: set[str] | None = None
        if server_name is not None:
            rows = self.connection.execute(
                "SELECT catalog_id FROM catalog_entries WHERE name = ?", (server_name,)
            ).fetchall()
            catalog_ids = {str(row["catalog_id"]) for row in rows}
        backends = RuntimeBackendStore(self.connection).list()
        if catalog_ids is not None:
            backends = [backend for backend in backends if backend.catalog_id in catalog_ids]
        return {
            "schema_version": 1,
            "kind": "MCPHubRuntimeStatus",
            "agent_id": agent_id,
            "server_name": server_name,
            "backends": [backend.to_dict() for backend in backends],
        }

    def credential_status(self, context: AuthContext) -> dict[str, Any]:
        """Return credential readiness without resolving secret values."""
        agent_id = _require_agent_context(context)
        active_catalog_ids = {
            str(row["catalog_id"])
            for row in self.connection.execute(
                """
                SELECT DISTINCT catalog_id
                FROM runtime_backends
                WHERE state IN ('starting', 'hot', 'idle')
                """
            ).fetchall()
        }
        credentials = CredentialRefStore(self.connection).list()
        summary = readiness_summary(credentials, active_catalog_ids=active_catalog_ids)
        return {
            "schema_version": 1,
            "kind": "MCPHubCredentialStatus",
            "agent_id": agent_id,
            "summary": summary.to_dict(),
        }

    def catalog_search(self, context: AuthContext, *, query: str | None = None) -> dict[str, Any]:
        """Search catalog entries by name, canonical name, display label, or alias."""
        agent_id = _require_agent_context(context)
        query_text = (query or "").strip().lower()
        entries = []
        for entry in CatalogStore(self.connection).list():
            haystack = " ".join(
                [entry.name, entry.canonical_name, entry.display_label, *entry.aliases]
            ).lower()
            if query_text and query_text not in haystack:
                continue
            entries.append(entry.to_dict())
        return {
            "schema_version": 1,
            "kind": "MCPHubCatalogSearch",
            "agent_id": agent_id,
            "query": query,
            "entries": entries,
        }

    def _auth_context(self, auth_token: str | None) -> AuthContext:
        if not auth_token:
            raise ControlMCPError("mcp_hub tool call requires an auth token")
        try:
            return AuthTokenStore(self.connection).verify_local_token(
                auth_token, required_scope=CONTROL_READ
            )
        except AuthError as error:
            raise ControlMCPError(str(error)) from error


def _tool_metadata() -> list[ControlMCPTool]:
    object_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    return [
        ControlMCPTool("self_check", "Return invoking-agent compliance state.", object_schema),
        ControlMCPTool("status", "Return invoking-agent status.", object_schema),
        ControlMCPTool(
            "plan_list",
            "List invoking-agent remediation plans.",
            {
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        ControlMCPTool(
            "plan_show",
            "Show one invoking-agent remediation plan.",
            {
                "type": "object",
                "properties": {"plan_id": {"type": "string"}},
                "required": ["plan_id"],
                "additionalProperties": False,
            },
        ),
        ControlMCPTool(
            "proxy_url",
            "Return the Hub URL for one server.",
            {
                "type": "object",
                "properties": {"server_name": {"type": "string"}},
                "required": ["server_name"],
                "additionalProperties": False,
            },
        ),
        ControlMCPTool(
            "runtime_status",
            "Return runtime backend status.",
            {
                "type": "object",
                "properties": {"server_name": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        ControlMCPTool("credential_status", "Return credential readiness state.", object_schema),
        ControlMCPTool(
            "catalog_search",
            "Search catalog entries.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
    ]


def _require_agent_context(context: AuthContext) -> str:
    if context.subject_type != "agent" or not context.subject_id:
        raise ControlMCPError("mcp_hub tools require an agent-scoped token")
    return context.subject_id


def _scope_status_to_agent(status: dict[str, Any], agent_id: str) -> dict[str, Any]:
    scoped = dict(status)
    scoped["blockers"] = _filter_issues(status.get("blockers", []), agent_id)
    scoped["warnings"] = _filter_issues(status.get("warnings", []), agent_id)
    scoped["notices"] = _filter_issues(status.get("notices", []), agent_id)
    summary = dict(status.get("summary", {}))
    summary["blockers"] = len(scoped["blockers"])
    summary["warnings"] = len(scoped["warnings"])
    summary["notices"] = len(scoped["notices"])
    scoped["summary"] = summary
    scoped["ok"] = not scoped["blockers"]
    return scoped


def _filter_issues(issues: Any, agent_id: str) -> list[dict[str, Any]]:
    if not isinstance(issues, list):
        return []
    result: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        issue_agent_id = issue.get("agent_id")
        if issue_agent_id is None or issue_agent_id == agent_id:
            result.append(dict(issue))
    return result


def _plan_rows(
    connection: sqlite3.Connection,
    *,
    agent_id: str,
    status: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["agent_id = ?"]
    params = [agent_id]
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    rows = connection.execute(
        f"""
        SELECT plan_id
        FROM remediation_plans
        WHERE {" AND ".join(clauses)}
        ORDER BY created_at, plan_id
        """,
        params,
    ).fetchall()
    return [_plan_row(connection, str(row["plan_id"])) for row in rows]


def _plan_row(connection: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT *
        FROM remediation_plans
        WHERE plan_id = ?
        """,
        (plan_id,),
    ).fetchone()
    if row is None:
        raise ControlMCPError(f"unknown plan: {plan_id}")
    return {
        "plan_id": str(row["plan_id"]),
        "schema_version": int(row["schema_version"]),
        "plan_type": str(row["plan_type"]),
        "status": str(row["status"]),
        "agent_id": row["agent_id"],
        "target_path": row["target_path"],
        "observed_entry_id": row["observed_entry_id"],
        "catalog_id": row["catalog_id"],
        "policy": json.loads(str(row["policy_json"])),
        "diff": {"format": str(row["diff_format"]), "text": str(row["diff_text"])},
        "expected_preimage_hash": row["expected_preimage_hash"],
        "rollback_strategy": row["rollback_strategy"],
        "risk": json.loads(str(row["risk_json"])),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _catalog_entry_by_name(connection: sqlite3.Connection, server_name: str) -> Any | None:
    row = connection.execute(
        """
        SELECT catalog_id
        FROM catalog_entries
        WHERE name = ?
        LIMIT 1
        """,
        (server_name,),
    ).fetchone()
    if row is None:
        return None
    return CatalogStore(connection).show(str(row["catalog_id"]))


def _optional_str(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ControlMCPError(f"{key} must be a string")
    return value


def _required_str(arguments: dict[str, Any], key: str) -> str:
    value = _optional_str(arguments, key)
    if not value:
        raise ControlMCPError(f"{key} is required")
    return value


__all__ = [
    "ControlMCPError",
    "ControlMCPServer",
    "ControlMCPTool",
    "TOOL_NAMES",
]
