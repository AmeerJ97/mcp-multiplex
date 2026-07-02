"""Agent registration records and identity hints."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mcp_multiplex.auth import AuthTokenStore, IssuedToken
from mcp_multiplex.storage import migrate

SUPPORTED_AGENT_KINDS = frozenset({"codex", "claude_code", "gemini", "cline", "opencode"})
CERTIFICATION_LEVELS = frozenset({"certified", "best_effort", "unverified", "unsupported"})


class AgentRegistryError(ValueError):
    """Raised for invalid agent registration input."""


@dataclass(frozen=True)
class AgentConfigPath:
    """Known config path for one registered agent."""

    path: str
    format: str
    precedence: int = 0
    is_project_shared: bool = False
    config_path_id: str | None = None


@dataclass(frozen=True)
class AgentRegistration:
    """Durable agent registration."""

    agent_id: str
    agent_kind: str
    display_name: str
    workspace_root: str | None = None
    config_paths: list[AgentConfigPath] = field(default_factory=list)
    control_plane_mount: str = "mcp_hub"
    auth_token_ref: str | None = None
    certification_level: str = "unverified"
    created_at: str | None = None
    last_seen_at: str | None = None

    @property
    def can_auto_remediate(self) -> bool:
        """Whether later remediation code may consider safe auto-apply."""
        return self.certification_level == "certified"


class AgentRegistry:
    """SQLite-backed registry for supported coding agents."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate(connection)

    def create(
        self,
        *,
        agent_id: str,
        agent_kind: str,
        display_name: str,
        workspace_root: str | Path | None = None,
        config_paths: list[AgentConfigPath] | None = None,
        control_plane_mount: str = "mcp_hub",
        auth_token_ref: str | None = None,
        certification_level: str = "unverified",
    ) -> AgentRegistration:
        """Create an agent registration and its config path hints."""
        self._validate_agent(
            agent_id=agent_id,
            agent_kind=agent_kind,
            display_name=display_name,
            control_plane_mount=control_plane_mount,
            certification_level=certification_level,
        )
        path_records = config_paths or []
        workspace = str(workspace_root) if workspace_root is not None else None
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO agents (
                  agent_id,
                  agent_kind,
                  display_name,
                  workspace_root,
                  control_plane_mount,
                  auth_token_ref,
                  certification_level
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    agent_kind,
                    display_name,
                    workspace,
                    control_plane_mount,
                    auth_token_ref,
                    certification_level,
                ),
            )
            for index, config_path in enumerate(path_records):
                self._validate_config_path(config_path)
                self.connection.execute(
                    """
                    INSERT INTO agent_config_paths (
                      config_path_id,
                      agent_id,
                      path,
                      precedence,
                      format,
                      is_project_shared
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        config_path.config_path_id or f"{agent_id}:path:{index}",
                        agent_id,
                        config_path.path,
                        config_path.precedence,
                        config_path.format,
                        int(config_path.is_project_shared),
                    ),
                )
        return self.show(agent_id)

    def upsert(
        self,
        *,
        agent_id: str,
        agent_kind: str,
        display_name: str,
        workspace_root: str | Path | None = None,
        config_paths: list[AgentConfigPath] | None = None,
        control_plane_mount: str = "mcp_hub",
        auth_token_ref: str | None = None,
        certification_level: str = "unverified",
    ) -> AgentRegistration:
        """Create or replace a registration while preserving stable identity fields."""
        self._validate_agent(
            agent_id=agent_id,
            agent_kind=agent_kind,
            display_name=display_name,
            control_plane_mount=control_plane_mount,
            certification_level=certification_level,
        )
        path_records = config_paths or []
        workspace = str(workspace_root) if workspace_root is not None else None
        existing = None
        try:
            existing = self.show(agent_id)
        except KeyError:
            existing = None
        if existing is not None:
            if auth_token_ref is None:
                auth_token_ref = existing.auth_token_ref
            if certification_level == "unverified":
                certification_level = existing.certification_level
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO agents (
                  agent_id,
                  agent_kind,
                  display_name,
                  workspace_root,
                  control_plane_mount,
                  auth_token_ref,
                  certification_level
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                  agent_kind = excluded.agent_kind,
                  display_name = excluded.display_name,
                  workspace_root = excluded.workspace_root,
                  control_plane_mount = excluded.control_plane_mount,
                  auth_token_ref = excluded.auth_token_ref,
                  certification_level = excluded.certification_level,
                  last_seen_at = CURRENT_TIMESTAMP
                """,
                (
                    agent_id,
                    agent_kind,
                    display_name,
                    workspace,
                    control_plane_mount,
                    auth_token_ref,
                    certification_level,
                ),
            )
            self.connection.execute(
                "DELETE FROM agent_config_paths WHERE agent_id = ?",
                (agent_id,),
            )
            for index, config_path in enumerate(path_records):
                self._validate_config_path(config_path)
                self.connection.execute(
                    """
                    INSERT INTO agent_config_paths (
                      config_path_id,
                      agent_id,
                      path,
                      precedence,
                      format,
                      is_project_shared
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        config_path.config_path_id or f"{agent_id}:path:{index}",
                        agent_id,
                        config_path.path,
                        config_path.precedence,
                        config_path.format,
                        int(config_path.is_project_shared),
                    ),
                )
        return self.show(agent_id)

    def list(self) -> list[AgentRegistration]:
        """List registered agents in stable order."""
        rows = self.connection.execute(
            "SELECT agent_id FROM agents ORDER BY agent_kind, agent_id"
        ).fetchall()
        return [self.show(str(row["agent_id"])) for row in rows]

    def issue_registration_token(
        self,
        agent_id: str,
        *,
        scopes: Sequence[str] | None = None,
        expires_at: str | None = None,
    ) -> IssuedToken:
        """Issue a one-time registration token for a known agent."""
        agent = self.show(agent_id)
        return AuthTokenStore(self.connection).issue_agent_registration_token(
            agent_id=agent.agent_id,
            agent_kind=agent.agent_kind,
            scopes=list(scopes) if scopes is not None else None,
            expires_at=expires_at,
        )

    def exchange_registration_token(self, token: str) -> IssuedToken:
        """Exchange a one-time agent registration token for a scoped auth token."""
        return AuthTokenStore(self.connection).exchange_agent_registration_token(token)

    def show(self, agent_id: str) -> AgentRegistration:
        """Return one agent registration."""
        row = self.connection.execute(
            """
            SELECT
              agent_id,
              agent_kind,
              display_name,
              workspace_root,
              control_plane_mount,
              auth_token_ref,
              certification_level,
              created_at,
              last_seen_at
            FROM agents
            WHERE agent_id = ?
            """,
            (agent_id,),
        ).fetchone()
        if row is None:
            raise KeyError(agent_id)
        path_rows = self.connection.execute(
            """
            SELECT config_path_id, path, format, precedence, is_project_shared
            FROM agent_config_paths
            WHERE agent_id = ?
            ORDER BY precedence, path
            """,
            (agent_id,),
        ).fetchall()
        return AgentRegistration(
            agent_id=str(row["agent_id"]),
            agent_kind=str(row["agent_kind"]),
            display_name=str(row["display_name"]),
            workspace_root=row["workspace_root"],
            config_paths=[
                AgentConfigPath(
                    config_path_id=str(path_row["config_path_id"]),
                    path=str(path_row["path"]),
                    format=str(path_row["format"]),
                    precedence=int(path_row["precedence"]),
                    is_project_shared=bool(path_row["is_project_shared"]),
                )
                for path_row in path_rows
            ],
            control_plane_mount=str(row["control_plane_mount"]),
            auth_token_ref=row["auth_token_ref"],
            certification_level=str(row["certification_level"]),
            created_at=str(row["created_at"]),
            last_seen_at=row["last_seen_at"],
        )

    def _validate_agent(
        self,
        *,
        agent_id: str,
        agent_kind: str,
        display_name: str,
        control_plane_mount: str,
        certification_level: str,
    ) -> None:
        if not agent_id:
            raise AgentRegistryError("agent_id is required")
        if agent_kind not in SUPPORTED_AGENT_KINDS:
            raise AgentRegistryError(f"unsupported agent_kind: {agent_kind}")
        if not display_name:
            raise AgentRegistryError("display_name is required")
        if control_plane_mount != "mcp_hub":
            raise AgentRegistryError("control_plane_mount must be mcp_hub")
        if certification_level not in CERTIFICATION_LEVELS:
            raise AgentRegistryError(f"unsupported certification_level: {certification_level}")

    def _validate_config_path(self, config_path: AgentConfigPath) -> None:
        if not config_path.path:
            raise AgentRegistryError("config path is required")
        if config_path.precedence < 0:
            raise AgentRegistryError("config path precedence must be >= 0")
        if config_path.format not in {"toml", "json", "yaml"}:
            raise AgentRegistryError(f"unsupported config path format: {config_path.format}")
