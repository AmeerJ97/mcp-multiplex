"""Control-plane installation helpers for supported agents."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp_multiplex.adapters import (
    AgentConfigPath,
    parse_claude_code_config,
    parse_cline_config,
    parse_codex_config,
    parse_gemini_config,
    parse_opencode_config,
)
from mcp_multiplex.apply import ConfigBackup, ConfigBackupStore, sha256_bytes
from mcp_multiplex.auth import CONTROL_READ, AuthTokenStore, IssuedToken
from mcp_multiplex.security import HUB_ORIGIN
from mcp_multiplex.storage import migrate

CODEX_AGENT_ID = "agent_codex_user_default"
CLAUDE_CODE_AGENT_ID = "agent_claude_user_default"
CLINE_AGENT_ID = "agent_cline_user_default"
GEMINI_AGENT_ID = "agent_gemini_user_default"
OPENCODE_AGENT_ID = "agent_opencode_user_default"
CODEX_CONTROL_TOKEN_ENV_VAR = "MCP_MULTIPLEX_CONTROL_TOKEN"
CLAUDE_CODE_CONTROL_HELPER = "claude-code-mcp-multiplex-headers.sh"
CLINE_CONTROL_HELPER = "cline-mcp-multiplex-remote.sh"
MCP_HUB_URL = f"{HUB_ORIGIN}/servers/mcp_hub/mcp"


class ControlPlaneInstallError(ValueError):
    """Raised when a control-plane install cannot be planned or applied safely."""


@dataclass(frozen=True)
class ControlPlaneInstallPreview:
    """Dry-run or applied control-plane install summary."""

    agent_id: str
    agent_kind: str
    target_path: str
    env_var: str
    url: str
    already_configured: bool
    would_change: bool
    before_hash: str
    after_hash: str
    backup: ConfigBackup | None = None
    helper_path: str | None = None
    helper_before_hash: str | None = None
    helper_after_hash: str | None = None
    helper_backup: ConfigBackup | None = None
    token: IssuedToken | None = None

    def to_dict(self, *, include_token: bool = False) -> dict[str, Any]:
        token_payload: dict[str, Any] | None = None
        if self.token is not None:
            token_payload = {
                "token_id": self.token.token_id,
                "token_ref": self.token.token_ref,
                "subject_type": self.token.subject_type,
                "subject_id": self.token.subject_id,
                "scopes": self.token.scopes,
                "expires_at": self.token.expires_at,
            }
            token_payload["token"] = self.token.token if include_token else "[REDACTED]"
        return {
            "agent_id": self.agent_id,
            "agent_kind": self.agent_kind,
            "target_path": self.target_path,
            "env_var": self.env_var,
            "url": self.url,
            "already_configured": self.already_configured,
            "would_change": self.would_change,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "backup": self.backup.to_dict() if self.backup is not None else None,
            "helper_path": self.helper_path,
            "helper_before_hash": self.helper_before_hash,
            "helper_after_hash": self.helper_after_hash,
            "helper_backup": (
                self.helper_backup.to_dict() if self.helper_backup is not None else None
            ),
            "token": token_payload,
            "operator_action": (
                f"Set {self.env_var} to the one-time token value before launching "
                f"{self.agent_kind}."
            ),
        }


@dataclass(frozen=True)
class ControlPlaneAuthCapability:
    """Safety classification for an agent's control-plane auth install path."""

    agent_kind: str
    automatic_install_supported: bool
    status: str
    auth_strategy: str
    raw_token_storage_required: bool
    implementation: str
    reasons: list[str]
    evidence: list[str]
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_kind": self.agent_kind,
            "automatic_install_supported": self.automatic_install_supported,
            "status": self.status,
            "auth_strategy": self.auth_strategy,
            "raw_token_storage_required": self.raw_token_storage_required,
            "implementation": self.implementation,
            "reasons": self.reasons,
            "evidence": self.evidence,
            "next_action": self.next_action,
        }


CONTROL_PLANE_AUTH_CAPABILITIES: dict[str, ControlPlaneAuthCapability] = {
    "codex": ControlPlaneAuthCapability(
        agent_kind="codex",
        automatic_install_supported=True,
        status="implemented",
        auth_strategy="bearer_token_env_var",
        raw_token_storage_required=False,
        implementation="mxp agents install-control-plane --agent codex",
        reasons=[
            "client supports bearer_token_env_var",
            "Multiplex writes only the env var name to durable config",
            "raw control token is emitted only on explicit request",
        ],
        evidence=[
            "OpenAI Codex config reference: mcp_servers.<name>.bearer_token_env_var",
            "tests/test_control_plane_install.py",
            "docs/certifications/TASK-038-codex-certification.md",
        ],
        next_action="supported now",
    ),
    "claude_code": ControlPlaneAuthCapability(
        agent_kind="claude_code",
        automatic_install_supported=True,
        status="implemented",
        auth_strategy="headersHelper",
        raw_token_storage_required=False,
        implementation="mxp agents install-control-plane --agent claude_code",
        reasons=[
            "Claude Code can generate headers dynamically with headersHelper",
            "Multiplex writes a helper path to durable config, not a raw token",
            "the helper reads the bearer token from MCP_MULTIPLEX_CONTROL_TOKEN at runtime",
        ],
        evidence=[
            "Claude Code MCP docs: dynamic headers through headersHelper",
            "docs/certifications/TASK-039-claude-code-certification.md",
            "tests/test_control_plane_install.py",
        ],
        next_action="supported now",
    ),
    "gemini": ControlPlaneAuthCapability(
        agent_kind="gemini",
        automatic_install_supported=True,
        status="implemented",
        auth_strategy="env_header_template",
        raw_token_storage_required=False,
        implementation="mxp agents install-control-plane --agent gemini",
        reasons=[
            "Gemini supports remote MCP headers in settings.json",
            "Multiplex writes a headers object containing Bearer $MCP_MULTIPLEX_CONTROL_TOKEN",
            "raw control tokens are emitted only on explicit request",
        ],
        evidence=[
            "Gemini CLI MCP docs: headers is an object for url/httpUrl transports",
            "docs/certifications/TASK-040-gemini-certification.md",
            "tests/test_control_plane_install.py",
        ],
        next_action="supported now",
    ),
    "cline": ControlPlaneAuthCapability(
        agent_kind="cline",
        automatic_install_supported=True,
        status="implemented",
        auth_strategy="stdio_mcp_remote_helper",
        raw_token_storage_required=False,
        implementation="mxp agents install-control-plane --agent cline",
        reasons=[
            "Cline can run stdio MCP commands",
            "Multiplex writes a helper script path to durable config, not a raw token",
            "the helper uses mcp-remote to call the Hub URL with a runtime bearer header",
        ],
        evidence=[
            "Cline MCP docs document command-based MCP server entries",
            "mcp-remote docs support custom --header arguments",
            "docs/certifications/TASK-041-cline-certification.md",
            "tests/test_control_plane_install.py",
        ],
        next_action="supported now",
    ),
    "opencode": ControlPlaneAuthCapability(
        agent_kind="opencode",
        automatic_install_supported=True,
        status="implemented",
        auth_strategy="env_header_template",
        raw_token_storage_required=False,
        implementation="mxp agents install-control-plane --agent opencode",
        reasons=[
            "OpenCode supports environment-variable expansion inside remote MCP headers",
            "Multiplex writes only Bearer {env:MCP_MULTIPLEX_CONTROL_TOKEN} to durable config",
            "OAuth is disabled for the local bearer-token control-plane entry",
        ],
        evidence=[
            "OpenCode MCP docs: headers may use {env:MY_API_KEY} with oauth disabled",
            "docs/certifications/TASK-042-opencode-certification.md",
            "tests/test_control_plane_install.py",
        ],
        next_action="supported now",
    ),
}


def control_plane_auth_capability(agent_kind: str) -> ControlPlaneAuthCapability:
    """Return the control-plane auth install safety classification for one agent."""
    try:
        return CONTROL_PLANE_AUTH_CAPABILITIES[agent_kind]
    except KeyError as error:
        raise ControlPlaneInstallError(f"unsupported agent kind: {agent_kind}") from error


def control_plane_auth_capabilities() -> list[ControlPlaneAuthCapability]:
    """List auth install capabilities in deterministic first-wave order."""
    return [
        CONTROL_PLANE_AUTH_CAPABILITIES[agent]
        for agent in ("codex", "claude_code", "gemini", "cline", "opencode")
    ]


def plan_codex_control_plane_install(
    *,
    home: Path | None = None,
    config_path: Path | None = None,
) -> ControlPlaneInstallPreview:
    """Return a non-mutating preview for the Codex control-plane MCP entry."""
    target = _codex_config_path(home=home, config_path=config_path)
    before_bytes = target.read_bytes() if target.exists() else b""
    before_hash = sha256_bytes(before_bytes)
    after_text = _rewrite_codex_control_plane(before_bytes.decode("utf-8") if before_bytes else "")
    after_hash = sha256_bytes(after_text.encode("utf-8"))
    return ControlPlaneInstallPreview(
        agent_id=CODEX_AGENT_ID,
        agent_kind="codex",
        target_path=str(target),
        env_var=CODEX_CONTROL_TOKEN_ENV_VAR,
        url=MCP_HUB_URL,
        already_configured=before_hash == after_hash and _codex_config_is_authenticated(target),
        would_change=before_hash != after_hash,
        before_hash=before_hash,
        after_hash=after_hash,
    )


def install_codex_control_plane(
    connection: sqlite3.Connection,
    *,
    home: Path | None = None,
    config_path: Path | None = None,
    backup_dir: Path | None = None,
    actor: str = "local_operator",
) -> ControlPlaneInstallPreview:
    """Install authenticated `mcp_hub` for Codex without writing raw token material."""
    del actor
    migrate(connection)
    target = _codex_config_path(home=home, config_path=config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    before_bytes = target.read_bytes() if target.exists() else b""
    before_hash = sha256_bytes(before_bytes)
    after_text = _rewrite_codex_control_plane(before_bytes.decode("utf-8") if before_bytes else "")
    after_bytes = after_text.encode("utf-8")
    after_hash = sha256_bytes(after_bytes)
    token = AuthTokenStore(connection).issue_local_token(
        subject_type="agent",
        subject_id=CODEX_AGENT_ID,
        scopes=[CONTROL_READ],
    )
    _upsert_codex_agent(connection, config_path=target, auth_token_ref=token.token_ref)
    plan_id = _install_plan_id(CODEX_AGENT_ID, str(target), before_hash)
    _upsert_install_plan(
        connection,
        plan_id=plan_id,
        agent_id=CODEX_AGENT_ID,
        target_path=target,
        before_hash=before_hash,
        after_hash=after_hash,
        auth_kind="bearer_token_env_var",
    )
    backup = ConfigBackupStore(connection).create(
        plan_id=plan_id,
        target_path=target,
        content=before_bytes,
        backup_dir=backup_dir,
    )
    if before_hash != after_hash:
        _atomic_write_text(target, after_text)
    _verify_codex_control_plane(target)
    return ControlPlaneInstallPreview(
        agent_id=CODEX_AGENT_ID,
        agent_kind="codex",
        target_path=str(target),
        env_var=CODEX_CONTROL_TOKEN_ENV_VAR,
        url=MCP_HUB_URL,
        already_configured=before_hash == after_hash,
        would_change=before_hash != after_hash,
        before_hash=before_hash,
        after_hash=after_hash,
        backup=backup,
        token=token,
    )


def plan_claude_code_control_plane_install(
    *,
    home: Path | None = None,
    config_path: Path | None = None,
    helper_path: Path | None = None,
) -> ControlPlaneInstallPreview:
    """Return a non-mutating preview for the Claude Code control-plane MCP entry."""
    target = _claude_code_config_path(home=home, config_path=config_path)
    helper = _claude_code_helper_path(home=home, helper_path=helper_path)
    before_bytes = target.read_bytes() if target.exists() else b""
    before_hash = sha256_bytes(before_bytes)
    after_text = _rewrite_claude_code_control_plane(
        before_bytes.decode("utf-8") if before_bytes else "",
        helper_path=helper,
    )
    after_hash = sha256_bytes(after_text.encode("utf-8"))
    helper_before = helper.read_bytes() if helper.exists() else b""
    helper_text = _claude_code_helper_script()
    return ControlPlaneInstallPreview(
        agent_id=CLAUDE_CODE_AGENT_ID,
        agent_kind="claude_code",
        target_path=str(target),
        env_var=CODEX_CONTROL_TOKEN_ENV_VAR,
        url=MCP_HUB_URL,
        already_configured=(
            before_hash == after_hash
            and sha256_bytes(helper_before) == sha256_bytes(helper_text.encode("utf-8"))
            and _claude_code_config_is_authenticated(target, helper_path=helper)
        ),
        would_change=(
            before_hash != after_hash
            or sha256_bytes(helper_before) != sha256_bytes(helper_text.encode("utf-8"))
        ),
        before_hash=before_hash,
        after_hash=after_hash,
        helper_path=str(helper),
        helper_before_hash=sha256_bytes(helper_before),
        helper_after_hash=sha256_bytes(helper_text.encode("utf-8")),
    )


def install_claude_code_control_plane(
    connection: sqlite3.Connection,
    *,
    home: Path | None = None,
    config_path: Path | None = None,
    helper_path: Path | None = None,
    backup_dir: Path | None = None,
    actor: str = "local_operator",
) -> ControlPlaneInstallPreview:
    """Install authenticated `mcp_hub` for Claude Code via a dynamic header helper."""
    del actor
    migrate(connection)
    target = _claude_code_config_path(home=home, config_path=config_path)
    helper = _claude_code_helper_path(home=home, helper_path=helper_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    helper.parent.mkdir(parents=True, exist_ok=True)
    before_bytes = target.read_bytes() if target.exists() else b""
    helper_before_bytes = helper.read_bytes() if helper.exists() else b""
    before_hash = sha256_bytes(before_bytes)
    helper_before_hash = sha256_bytes(helper_before_bytes)
    after_text = _rewrite_claude_code_control_plane(
        before_bytes.decode("utf-8") if before_bytes else "",
        helper_path=helper,
    )
    after_bytes = after_text.encode("utf-8")
    after_hash = sha256_bytes(after_bytes)
    helper_text = _claude_code_helper_script()
    helper_bytes = helper_text.encode("utf-8")
    helper_after_hash = sha256_bytes(helper_bytes)
    token = AuthTokenStore(connection).issue_local_token(
        subject_type="agent",
        subject_id=CLAUDE_CODE_AGENT_ID,
        scopes=[CONTROL_READ],
    )
    _upsert_claude_code_agent(connection, config_path=target, auth_token_ref=token.token_ref)
    plan_id = _install_plan_id(CLAUDE_CODE_AGENT_ID, str(target), before_hash)
    _upsert_install_plan(
        connection,
        plan_id=plan_id,
        agent_id=CLAUDE_CODE_AGENT_ID,
        target_path=target,
        before_hash=before_hash,
        after_hash=after_hash,
        auth_kind="headersHelper",
        helper_path=helper,
    )
    store = ConfigBackupStore(connection)
    backup = store.create(
        plan_id=plan_id,
        target_path=target,
        content=before_bytes,
        backup_dir=backup_dir,
    )
    helper_backup = store.create(
        plan_id=plan_id,
        target_path=helper,
        content=helper_before_bytes,
        backup_dir=backup_dir,
    )
    if before_hash != after_hash:
        _atomic_write_text(target, after_text)
    if helper_before_hash != helper_after_hash:
        _atomic_write_text(helper, helper_text)
    helper.chmod(0o700)
    _verify_claude_code_control_plane(target, helper_path=helper)
    return ControlPlaneInstallPreview(
        agent_id=CLAUDE_CODE_AGENT_ID,
        agent_kind="claude_code",
        target_path=str(target),
        env_var=CODEX_CONTROL_TOKEN_ENV_VAR,
        url=MCP_HUB_URL,
        already_configured=before_hash == after_hash and helper_before_hash == helper_after_hash,
        would_change=before_hash != after_hash or helper_before_hash != helper_after_hash,
        before_hash=before_hash,
        after_hash=after_hash,
        backup=backup,
        helper_path=str(helper),
        helper_before_hash=helper_before_hash,
        helper_after_hash=helper_after_hash,
        helper_backup=helper_backup,
        token=token,
    )


def plan_cline_control_plane_install(
    *,
    home: Path | None = None,
    config_path: Path | None = None,
    helper_path: Path | None = None,
) -> ControlPlaneInstallPreview:
    """Return a non-mutating preview for the Cline control-plane MCP entry."""
    target = _cline_config_path(home=home, config_path=config_path)
    helper = _cline_helper_path(home=home, helper_path=helper_path)
    before_bytes = target.read_bytes() if target.exists() else b""
    before_hash = sha256_bytes(before_bytes)
    after_text = _rewrite_cline_control_plane(
        before_bytes.decode("utf-8") if before_bytes else "",
        helper_path=helper,
    )
    after_hash = sha256_bytes(after_text.encode("utf-8"))
    helper_before = helper.read_bytes() if helper.exists() else b""
    helper_text = _cline_helper_script()
    return ControlPlaneInstallPreview(
        agent_id=CLINE_AGENT_ID,
        agent_kind="cline",
        target_path=str(target),
        env_var=CODEX_CONTROL_TOKEN_ENV_VAR,
        url=MCP_HUB_URL,
        already_configured=(
            before_hash == after_hash
            and sha256_bytes(helper_before) == sha256_bytes(helper_text.encode("utf-8"))
            and _cline_config_is_authenticated(target, helper_path=helper)
        ),
        would_change=(
            before_hash != after_hash
            or sha256_bytes(helper_before) != sha256_bytes(helper_text.encode("utf-8"))
        ),
        before_hash=before_hash,
        after_hash=after_hash,
        helper_path=str(helper),
        helper_before_hash=sha256_bytes(helper_before),
        helper_after_hash=sha256_bytes(helper_text.encode("utf-8")),
    )


def install_cline_control_plane(
    connection: sqlite3.Connection,
    *,
    home: Path | None = None,
    config_path: Path | None = None,
    helper_path: Path | None = None,
    backup_dir: Path | None = None,
    actor: str = "local_operator",
) -> ControlPlaneInstallPreview:
    """Install authenticated `mcp_hub` for Cline through a local mcp-remote helper."""
    del actor
    migrate(connection)
    target = _cline_config_path(home=home, config_path=config_path)
    helper = _cline_helper_path(home=home, helper_path=helper_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    helper.parent.mkdir(parents=True, exist_ok=True)
    before_bytes = target.read_bytes() if target.exists() else b""
    helper_before_bytes = helper.read_bytes() if helper.exists() else b""
    before_hash = sha256_bytes(before_bytes)
    helper_before_hash = sha256_bytes(helper_before_bytes)
    after_text = _rewrite_cline_control_plane(
        before_bytes.decode("utf-8") if before_bytes else "",
        helper_path=helper,
    )
    after_hash = sha256_bytes(after_text.encode("utf-8"))
    helper_text = _cline_helper_script()
    helper_after_hash = sha256_bytes(helper_text.encode("utf-8"))
    token = AuthTokenStore(connection).issue_local_token(
        subject_type="agent",
        subject_id=CLINE_AGENT_ID,
        scopes=[CONTROL_READ],
    )
    _upsert_cline_agent(connection, config_path=target, auth_token_ref=token.token_ref)
    plan_id = _install_plan_id(CLINE_AGENT_ID, str(target), before_hash)
    _upsert_install_plan(
        connection,
        plan_id=plan_id,
        agent_id=CLINE_AGENT_ID,
        target_path=target,
        before_hash=before_hash,
        after_hash=after_hash,
        auth_kind="stdio_mcp_remote_helper",
        helper_path=helper,
    )
    store = ConfigBackupStore(connection)
    backup = store.create(
        plan_id=plan_id,
        target_path=target,
        content=before_bytes,
        backup_dir=backup_dir,
    )
    helper_backup = store.create(
        plan_id=plan_id,
        target_path=helper,
        content=helper_before_bytes,
        backup_dir=backup_dir,
    )
    if before_hash != after_hash:
        _atomic_write_text(target, after_text)
    if helper_before_hash != helper_after_hash:
        _atomic_write_text(helper, helper_text)
    helper.chmod(0o700)
    _verify_cline_control_plane(target, helper_path=helper)
    return ControlPlaneInstallPreview(
        agent_id=CLINE_AGENT_ID,
        agent_kind="cline",
        target_path=str(target),
        env_var=CODEX_CONTROL_TOKEN_ENV_VAR,
        url=MCP_HUB_URL,
        already_configured=before_hash == after_hash and helper_before_hash == helper_after_hash,
        would_change=before_hash != after_hash or helper_before_hash != helper_after_hash,
        before_hash=before_hash,
        after_hash=after_hash,
        backup=backup,
        helper_path=str(helper),
        helper_before_hash=helper_before_hash,
        helper_after_hash=helper_after_hash,
        helper_backup=helper_backup,
        token=token,
    )


def plan_gemini_control_plane_install(
    *,
    home: Path | None = None,
    config_path: Path | None = None,
) -> ControlPlaneInstallPreview:
    """Return a non-mutating preview for the Gemini control-plane MCP entry."""
    target = _gemini_config_path(home=home, config_path=config_path)
    before_bytes = target.read_bytes() if target.exists() else b""
    before_hash = sha256_bytes(before_bytes)
    after_text = _rewrite_gemini_control_plane(before_bytes.decode("utf-8") if before_bytes else "")
    after_hash = sha256_bytes(after_text.encode("utf-8"))
    return ControlPlaneInstallPreview(
        agent_id=GEMINI_AGENT_ID,
        agent_kind="gemini",
        target_path=str(target),
        env_var=CODEX_CONTROL_TOKEN_ENV_VAR,
        url=MCP_HUB_URL,
        already_configured=before_hash == after_hash and _gemini_config_is_authenticated(target),
        would_change=before_hash != after_hash,
        before_hash=before_hash,
        after_hash=after_hash,
    )


def install_gemini_control_plane(
    connection: sqlite3.Connection,
    *,
    home: Path | None = None,
    config_path: Path | None = None,
    backup_dir: Path | None = None,
    actor: str = "local_operator",
) -> ControlPlaneInstallPreview:
    """Install authenticated `mcp_hub` for Gemini without writing raw token material."""
    del actor
    migrate(connection)
    target = _gemini_config_path(home=home, config_path=config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    before_bytes = target.read_bytes() if target.exists() else b""
    before_hash = sha256_bytes(before_bytes)
    after_text = _rewrite_gemini_control_plane(before_bytes.decode("utf-8") if before_bytes else "")
    after_bytes = after_text.encode("utf-8")
    after_hash = sha256_bytes(after_bytes)
    token = AuthTokenStore(connection).issue_local_token(
        subject_type="agent",
        subject_id=GEMINI_AGENT_ID,
        scopes=[CONTROL_READ],
    )
    _upsert_gemini_agent(connection, config_path=target, auth_token_ref=token.token_ref)
    plan_id = _install_plan_id(GEMINI_AGENT_ID, str(target), before_hash)
    _upsert_install_plan(
        connection,
        plan_id=plan_id,
        agent_id=GEMINI_AGENT_ID,
        target_path=target,
        before_hash=before_hash,
        after_hash=after_hash,
        auth_kind="env_header_template",
    )
    backup = ConfigBackupStore(connection).create(
        plan_id=plan_id,
        target_path=target,
        content=before_bytes,
        backup_dir=backup_dir,
    )
    if before_hash != after_hash:
        _atomic_write_text(target, after_text)
    _verify_gemini_control_plane(target)
    return ControlPlaneInstallPreview(
        agent_id=GEMINI_AGENT_ID,
        agent_kind="gemini",
        target_path=str(target),
        env_var=CODEX_CONTROL_TOKEN_ENV_VAR,
        url=MCP_HUB_URL,
        already_configured=before_hash == after_hash,
        would_change=before_hash != after_hash,
        before_hash=before_hash,
        after_hash=after_hash,
        backup=backup,
        token=token,
    )


def plan_opencode_control_plane_install(
    *,
    home: Path | None = None,
    config_path: Path | None = None,
) -> ControlPlaneInstallPreview:
    """Return a non-mutating preview for the OpenCode control-plane MCP entry."""
    target = _opencode_config_path(home=home, config_path=config_path)
    before_bytes = target.read_bytes() if target.exists() else b""
    before_hash = sha256_bytes(before_bytes)
    after_text = _rewrite_opencode_control_plane(
        before_bytes.decode("utf-8") if before_bytes else ""
    )
    after_hash = sha256_bytes(after_text.encode("utf-8"))
    return ControlPlaneInstallPreview(
        agent_id=OPENCODE_AGENT_ID,
        agent_kind="opencode",
        target_path=str(target),
        env_var=CODEX_CONTROL_TOKEN_ENV_VAR,
        url=MCP_HUB_URL,
        already_configured=before_hash == after_hash and _opencode_config_is_authenticated(target),
        would_change=before_hash != after_hash,
        before_hash=before_hash,
        after_hash=after_hash,
    )


def install_opencode_control_plane(
    connection: sqlite3.Connection,
    *,
    home: Path | None = None,
    config_path: Path | None = None,
    backup_dir: Path | None = None,
    actor: str = "local_operator",
) -> ControlPlaneInstallPreview:
    """Install authenticated `mcp_hub` for OpenCode without writing raw token material."""
    del actor
    migrate(connection)
    target = _opencode_config_path(home=home, config_path=config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    before_bytes = target.read_bytes() if target.exists() else b""
    before_hash = sha256_bytes(before_bytes)
    after_text = _rewrite_opencode_control_plane(
        before_bytes.decode("utf-8") if before_bytes else ""
    )
    after_bytes = after_text.encode("utf-8")
    after_hash = sha256_bytes(after_bytes)
    token = AuthTokenStore(connection).issue_local_token(
        subject_type="agent",
        subject_id=OPENCODE_AGENT_ID,
        scopes=[CONTROL_READ],
    )
    _upsert_opencode_agent(connection, config_path=target, auth_token_ref=token.token_ref)
    plan_id = _install_plan_id(OPENCODE_AGENT_ID, str(target), before_hash)
    _upsert_install_plan(
        connection,
        plan_id=plan_id,
        agent_id=OPENCODE_AGENT_ID,
        target_path=target,
        before_hash=before_hash,
        after_hash=after_hash,
        auth_kind="env_header_template",
    )
    backup = ConfigBackupStore(connection).create(
        plan_id=plan_id,
        target_path=target,
        content=before_bytes,
        backup_dir=backup_dir,
    )
    if before_hash != after_hash:
        _atomic_write_text(target, after_text)
    _verify_opencode_control_plane(target)
    return ControlPlaneInstallPreview(
        agent_id=OPENCODE_AGENT_ID,
        agent_kind="opencode",
        target_path=str(target),
        env_var=CODEX_CONTROL_TOKEN_ENV_VAR,
        url=MCP_HUB_URL,
        already_configured=before_hash == after_hash,
        would_change=before_hash != after_hash,
        before_hash=before_hash,
        after_hash=after_hash,
        backup=backup,
        token=token,
    )


def _codex_config_path(*, home: Path | None, config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path.expanduser().resolve()
    resolved_home = (home or Path.home()).expanduser()
    return (resolved_home / ".codex" / "config.toml").resolve()


def _claude_code_config_path(*, home: Path | None, config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path.expanduser().resolve()
    resolved_home = (home or Path.home()).expanduser()
    return (resolved_home / ".claude.json").resolve()


def _claude_code_helper_path(*, home: Path | None, helper_path: Path | None) -> Path:
    if helper_path is not None:
        return helper_path.expanduser().resolve()
    resolved_home = (home or Path.home()).expanduser()
    return (resolved_home / ".mcp-multiplex" / CLAUDE_CODE_CONTROL_HELPER).resolve()


def _cline_config_path(*, home: Path | None, config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path.expanduser().resolve()
    resolved_home = (home or Path.home()).expanduser()
    return (resolved_home / ".cline" / "data" / "settings" / "cline_mcp_settings.json").resolve()


def _cline_helper_path(*, home: Path | None, helper_path: Path | None) -> Path:
    if helper_path is not None:
        resolved = helper_path.expanduser().resolve()
        if resolved.name != CLINE_CONTROL_HELPER:
            raise ControlPlaneInstallError(
                f"Cline helper path must be named {CLINE_CONTROL_HELPER}"
            )
        return resolved
    resolved_home = (home or Path.home()).expanduser()
    return (resolved_home / ".mcp-multiplex" / CLINE_CONTROL_HELPER).resolve()


def _gemini_config_path(*, home: Path | None, config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path.expanduser().resolve()
    resolved_home = (home or Path.home()).expanduser()
    return (resolved_home / ".gemini" / "settings.json").resolve()


def _opencode_config_path(*, home: Path | None, config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path.expanduser().resolve()
    resolved_home = (home or Path.home()).expanduser()
    return (resolved_home / ".config" / "opencode" / "opencode.json").resolve()


def _rewrite_codex_control_plane(text: str) -> str:
    if text.strip():
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise ControlPlaneInstallError(f"invalid Codex TOML: {error}") from error
        servers = parsed.get("mcp_servers", {})
        if servers is not None and not isinstance(servers, dict):
            raise ControlPlaneInstallError("Codex mcp_servers must be a TOML table")
    normalized = _remove_codex_mcp_hub_block(text)
    prefix = normalized.rstrip()
    block = _codex_mcp_hub_block()
    return f"{prefix}\n\n{block}" if prefix else block


def _remove_codex_mcp_hub_block(text: str) -> str:
    pattern = re.compile(r"(?ms)^\[mcp_servers\.mcp_hub\]\n.*?(?=^\[|\Z)")
    return pattern.sub("", text).strip()


def _codex_mcp_hub_block() -> str:
    return (
        "[mcp_servers.mcp_hub]\n"
        f'url = "{MCP_HUB_URL}"\n'
        f'bearer_token_env_var = "{CODEX_CONTROL_TOKEN_ENV_VAR}"\n'
    )


def _rewrite_claude_code_control_plane(text: str, *, helper_path: Path) -> str:
    if text.strip():
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise ControlPlaneInstallError(f"invalid Claude Code JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise ControlPlaneInstallError("Claude Code config root must be an object")
    else:
        parsed = {}
    servers = parsed.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ControlPlaneInstallError("Claude Code mcpServers must be an object")
    servers["mcp_hub"] = {
        "type": "http",
        "url": MCP_HUB_URL,
        "headersHelper": str(helper_path),
    }
    return json.dumps(parsed, indent=2, sort_keys=True) + "\n"


def _rewrite_cline_control_plane(text: str, *, helper_path: Path) -> str:
    if text.strip():
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise ControlPlaneInstallError(f"invalid Cline JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise ControlPlaneInstallError("Cline config root must be an object")
    else:
        parsed = {}
    servers = parsed.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ControlPlaneInstallError("Cline mcpServers must be an object")
    servers["mcp_hub"] = {
        "command": str(helper_path),
        "args": [],
        "disabled": False,
        "autoApprove": [],
    }
    return json.dumps(parsed, indent=2, sort_keys=True) + "\n"


def _rewrite_gemini_control_plane(text: str) -> str:
    if text.strip():
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise ControlPlaneInstallError(f"invalid Gemini JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise ControlPlaneInstallError("Gemini config root must be an object")
    else:
        parsed = {}
    servers = parsed.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ControlPlaneInstallError("Gemini mcpServers must be an object")
    servers["mcp_hub"] = {
        "httpUrl": MCP_HUB_URL,
        "headers": {
            "Authorization": f"Bearer ${CODEX_CONTROL_TOKEN_ENV_VAR}",
        },
        "trust": False,
    }
    return json.dumps(parsed, indent=2, sort_keys=True) + "\n"


def _rewrite_opencode_control_plane(text: str) -> str:
    if text.strip():
        try:
            parsed = json.loads(_strip_jsonc_comments(text))
        except json.JSONDecodeError as error:
            raise ControlPlaneInstallError(f"invalid OpenCode JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise ControlPlaneInstallError("OpenCode config root must be an object")
    else:
        parsed = {"$schema": "https://opencode.ai/config.json"}
    servers = parsed.setdefault("mcp", {})
    if not isinstance(servers, dict):
        raise ControlPlaneInstallError("OpenCode mcp must be an object")
    servers["mcp_hub"] = {
        "type": "remote",
        "url": MCP_HUB_URL,
        "oauth": False,
        "headers": {
            "Authorization": f"Bearer {{env:{CODEX_CONTROL_TOKEN_ENV_VAR}}}",
        },
    }
    return json.dumps(parsed, indent=2, sort_keys=True) + "\n"


def _claude_code_helper_script() -> str:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f'if [ -z "${{{CODEX_CONTROL_TOKEN_ENV_VAR}:-}}" ]; then\n'
        f'  echo "{CODEX_CONTROL_TOKEN_ENV_VAR} is required" >&2\n'
        "  exit 1\n"
        "fi\n"
        'printf \'{"Authorization":"Bearer %s"}\\n\' '
        f'"${{{CODEX_CONTROL_TOKEN_ENV_VAR}}}"\n'
    )


def _cline_helper_script() -> str:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f'if [ -z "${{{CODEX_CONTROL_TOKEN_ENV_VAR}:-}}" ]; then\n'
        f'  echo "{CODEX_CONTROL_TOKEN_ENV_VAR} is required" >&2\n'
        "  exit 1\n"
        "fi\n"
        "exec npx -y mcp-remote "
        f'"{MCP_HUB_URL}" '
        f'--header "Authorization: Bearer ${{{CODEX_CONTROL_TOKEN_ENV_VAR}}}"\n'
    )


def _codex_config_is_authenticated(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        parsed = parse_codex_config(path)
    except Exception:
        return False
    return any(
        entry.mount_name == "mcp_hub"
        and entry.url == MCP_HUB_URL
        and "Authorization" in entry.headers_present
        for entry in parsed.observed_entries
    )


def _claude_code_config_is_authenticated(path: Path, *, helper_path: Path) -> bool:
    if not path.exists():
        return False
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        observed = parse_claude_code_config(path)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    servers = parsed.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    hub = servers.get("mcp_hub")
    if not isinstance(hub, dict):
        return False
    expected_helper = str(helper_path)
    return (
        hub.get("url") == MCP_HUB_URL
        and hub.get("headersHelper") == expected_helper
        and any(
            entry.mount_name == "mcp_hub"
            and entry.url == MCP_HUB_URL
            and "Authorization" in entry.headers_present
            for entry in observed.observed_entries
        )
    )


def _cline_config_is_authenticated(path: Path, *, helper_path: Path) -> bool:
    if not path.exists():
        return False
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        observed = parse_cline_config(path)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    servers = parsed.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    hub = servers.get("mcp_hub")
    if not isinstance(hub, dict):
        return False
    expected_helper = str(helper_path)
    return (
        hub.get("command") == expected_helper
        and hub.get("args") == []
        and any(
            entry.mount_name == "mcp_hub"
            and entry.url == MCP_HUB_URL
            and "Authorization" in entry.headers_present
            for entry in observed.observed_entries
        )
    )


def _gemini_config_is_authenticated(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        observed = parse_gemini_config(path)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    servers = parsed.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    hub = servers.get("mcp_hub")
    if not isinstance(hub, dict):
        return False
    return (
        hub.get("httpUrl") == MCP_HUB_URL
        and hub.get("headers") == {"Authorization": f"Bearer ${CODEX_CONTROL_TOKEN_ENV_VAR}"}
        and any(
            entry.mount_name == "mcp_hub"
            and entry.url == MCP_HUB_URL
            and "Authorization" in entry.headers_present
            for entry in observed.observed_entries
        )
    )


def _opencode_config_is_authenticated(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        parsed = json.loads(_strip_jsonc_comments(path.read_text(encoding="utf-8")))
        observed = parse_opencode_config(path)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    servers = parsed.get("mcp")
    if not isinstance(servers, dict):
        return False
    hub = servers.get("mcp_hub")
    if not isinstance(hub, dict):
        return False
    headers = hub.get("headers")
    return (
        hub.get("type") == "remote"
        and hub.get("url") == MCP_HUB_URL
        and hub.get("oauth") is False
        and isinstance(headers, dict)
        and headers.get("Authorization") == f"Bearer {{env:{CODEX_CONTROL_TOKEN_ENV_VAR}}}"
        and any(
            entry.mount_name == "mcp_hub"
            and entry.url == MCP_HUB_URL
            and "Authorization" in entry.headers_present
            for entry in observed.observed_entries
        )
    )


def _verify_codex_control_plane(path: Path) -> None:
    parsed = parse_codex_config(path)
    matches = [
        entry
        for entry in parsed.observed_entries
        if entry.mount_name == "mcp_hub"
        and entry.url == MCP_HUB_URL
        and "Authorization" in entry.headers_present
    ]
    if not matches:
        raise ControlPlaneInstallError("post-install verification failed for Codex mcp_hub")


def _verify_claude_code_control_plane(path: Path, *, helper_path: Path) -> None:
    if not _claude_code_config_is_authenticated(path, helper_path=helper_path):
        raise ControlPlaneInstallError("post-install verification failed for Claude Code mcp_hub")
    if not helper_path.exists():
        raise ControlPlaneInstallError("Claude Code headersHelper was not written")
    if helper_path.read_text(encoding="utf-8") != _claude_code_helper_script():
        raise ControlPlaneInstallError("Claude Code headersHelper content mismatch")


def _verify_cline_control_plane(path: Path, *, helper_path: Path) -> None:
    if not _cline_config_is_authenticated(path, helper_path=helper_path):
        raise ControlPlaneInstallError("post-install verification failed for Cline mcp_hub")
    if not helper_path.exists():
        raise ControlPlaneInstallError("Cline mcp-remote helper was not written")
    if helper_path.read_text(encoding="utf-8") != _cline_helper_script():
        raise ControlPlaneInstallError("Cline mcp-remote helper content mismatch")


def _verify_gemini_control_plane(path: Path) -> None:
    if not _gemini_config_is_authenticated(path):
        raise ControlPlaneInstallError("post-install verification failed for Gemini mcp_hub")


def _verify_opencode_control_plane(path: Path) -> None:
    if not _opencode_config_is_authenticated(path):
        raise ControlPlaneInstallError("post-install verification failed for OpenCode mcp_hub")


def _strip_jsonc_comments(config_text: str) -> str:
    result: list[str] = []
    index = 0
    in_string = False
    escape = False
    while index < len(config_text):
        char = config_text[index]
        next_char = config_text[index + 1] if index + 1 < len(config_text) else ""
        if in_string:
            result.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(config_text) and config_text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(config_text) and config_text[index : index + 2] != "*/":
                result.append("\n" if config_text[index] in "\r\n" else " ")
                index += 1
            index += 2 if index + 1 < len(config_text) else 0
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _upsert_install_plan(
    connection: sqlite3.Connection,
    *,
    plan_id: str,
    agent_id: str,
    target_path: Path,
    before_hash: str,
    after_hash: str,
    auth_kind: str,
    helper_path: Path | None = None,
) -> None:
    policy: dict[str, Any] = {
        "auto_apply_allowed": False,
        "approval_required": False,
        "approval_reason": "operator_requested_control_plane_install",
        "after": {
            "mcp_hub": {
                "transport": "streamable_http",
                "url": MCP_HUB_URL,
                "role": "control_plane",
                "auth": {
                    "kind": auth_kind,
                    "env_var": CODEX_CONTROL_TOKEN_ENV_VAR,
                    "token_ref_required": True,
                },
                "headers_present": ["Authorization"],
            }
        },
    }
    if helper_path is not None:
        policy["after"]["mcp_hub"]["auth"]["helper_path"] = str(helper_path)
    risk = {
        "tier": "normal",
        "verification": "parse_config_and_confirm_authenticated_mcp_hub_entry",
        "rollback": "restore_backup_before_apply",
    }
    with connection:
        connection.execute(
            """
            INSERT INTO remediation_plans (
              plan_id,
              schema_version,
              plan_type,
              status,
              agent_id,
              target_path,
              policy_json,
              diff_format,
              diff_text,
              expected_preimage_hash,
              rollback_strategy,
              risk_json
            )
            VALUES (?, 1, 'install_missing_control_plane', 'approved', ?, ?,
                    ?, 'summary', ?, ?,
                    'restore_backup', ?)
            ON CONFLICT(plan_id) DO UPDATE SET
              status = 'approved',
              policy_json = excluded.policy_json,
              diff_text = excluded.diff_text,
              expected_preimage_hash = excluded.expected_preimage_hash,
              risk_json = excluded.risk_json,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                plan_id,
                agent_id,
                str(target_path),
                _json(policy),
                f"install authenticated mcp_hub: {before_hash} -> {after_hash}",
                before_hash,
                _json(risk),
            ),
        )


def _upsert_codex_agent(
    connection: sqlite3.Connection,
    *,
    config_path: Path,
    auth_token_ref: str,
) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO agents (
              agent_id,
              agent_kind,
              display_name,
              control_plane_mount,
              auth_token_ref,
              certification_level
            )
            VALUES (?, 'codex', 'Codex CLI', 'mcp_hub', ?, 'certified')
            ON CONFLICT(agent_id) DO UPDATE SET
              auth_token_ref = excluded.auth_token_ref,
              certification_level = 'certified',
              last_seen_at = CURRENT_TIMESTAMP
            """,
            (CODEX_AGENT_ID, auth_token_ref),
        )
        config_record = AgentConfigPath(
            path=str(config_path),
            format="toml",
            precedence=10,
        )
        connection.execute(
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
            ON CONFLICT(agent_id, path) DO UPDATE SET
              precedence = excluded.precedence,
              format = excluded.format,
              is_project_shared = excluded.is_project_shared
            """,
            (
                f"{CODEX_AGENT_ID}:path:control-plane",
                CODEX_AGENT_ID,
                config_record.path,
                config_record.precedence,
                config_record.format,
                int(config_record.is_project_shared),
            ),
        )


def _upsert_claude_code_agent(
    connection: sqlite3.Connection,
    *,
    config_path: Path,
    auth_token_ref: str,
) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO agents (
              agent_id,
              agent_kind,
              display_name,
              control_plane_mount,
              auth_token_ref,
              certification_level
            )
            VALUES (?, 'claude_code', 'Claude Code', 'mcp_hub', ?, 'certified')
            ON CONFLICT(agent_id) DO UPDATE SET
              auth_token_ref = excluded.auth_token_ref,
              certification_level = 'certified',
              last_seen_at = CURRENT_TIMESTAMP
            """,
            (CLAUDE_CODE_AGENT_ID, auth_token_ref),
        )
        config_record = AgentConfigPath(
            path=str(config_path),
            format="json",
            precedence=10,
        )
        connection.execute(
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
            ON CONFLICT(agent_id, path) DO UPDATE SET
              precedence = excluded.precedence,
              format = excluded.format,
              is_project_shared = excluded.is_project_shared
            """,
            (
                f"{CLAUDE_CODE_AGENT_ID}:path:control-plane",
                CLAUDE_CODE_AGENT_ID,
                config_record.path,
                config_record.precedence,
                config_record.format,
                int(config_record.is_project_shared),
            ),
        )


def _upsert_cline_agent(
    connection: sqlite3.Connection,
    *,
    config_path: Path,
    auth_token_ref: str,
) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO agents (
              agent_id,
              agent_kind,
              display_name,
              control_plane_mount,
              auth_token_ref,
              certification_level
            )
            VALUES (?, 'cline', 'Cline', 'mcp_hub', ?, 'certified')
            ON CONFLICT(agent_id) DO UPDATE SET
              auth_token_ref = excluded.auth_token_ref,
              certification_level = 'certified',
              last_seen_at = CURRENT_TIMESTAMP
            """,
            (CLINE_AGENT_ID, auth_token_ref),
        )
        config_record = AgentConfigPath(
            path=str(config_path),
            format="json",
            precedence=10,
        )
        connection.execute(
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
            ON CONFLICT(agent_id, path) DO UPDATE SET
              precedence = excluded.precedence,
              format = excluded.format,
              is_project_shared = excluded.is_project_shared
            """,
            (
                f"{CLINE_AGENT_ID}:path:control-plane",
                CLINE_AGENT_ID,
                config_record.path,
                config_record.precedence,
                config_record.format,
                int(config_record.is_project_shared),
            ),
        )


def _upsert_gemini_agent(
    connection: sqlite3.Connection,
    *,
    config_path: Path,
    auth_token_ref: str,
) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO agents (
              agent_id,
              agent_kind,
              display_name,
              control_plane_mount,
              auth_token_ref,
              certification_level
            )
            VALUES (?, 'gemini', 'Gemini CLI', 'mcp_hub', ?, 'certified')
            ON CONFLICT(agent_id) DO UPDATE SET
              auth_token_ref = excluded.auth_token_ref,
              certification_level = 'certified',
              last_seen_at = CURRENT_TIMESTAMP
            """,
            (GEMINI_AGENT_ID, auth_token_ref),
        )
        config_record = AgentConfigPath(
            path=str(config_path),
            format="json",
            precedence=10,
        )
        connection.execute(
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
            ON CONFLICT(agent_id, path) DO UPDATE SET
              precedence = excluded.precedence,
              format = excluded.format,
              is_project_shared = excluded.is_project_shared
            """,
            (
                f"{GEMINI_AGENT_ID}:path:control-plane",
                GEMINI_AGENT_ID,
                config_record.path,
                config_record.precedence,
                config_record.format,
                int(config_record.is_project_shared),
            ),
        )


def _upsert_opencode_agent(
    connection: sqlite3.Connection,
    *,
    config_path: Path,
    auth_token_ref: str,
) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO agents (
              agent_id,
              agent_kind,
              display_name,
              control_plane_mount,
              auth_token_ref,
              certification_level
            )
            VALUES (?, 'opencode', 'OpenCode', 'mcp_hub', ?, 'certified')
            ON CONFLICT(agent_id) DO UPDATE SET
              auth_token_ref = excluded.auth_token_ref,
              certification_level = 'certified',
              last_seen_at = CURRENT_TIMESTAMP
            """,
            (OPENCODE_AGENT_ID, auth_token_ref),
        )
        config_record = AgentConfigPath(
            path=str(config_path),
            format="json",
            precedence=10,
        )
        connection.execute(
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
            ON CONFLICT(agent_id, path) DO UPDATE SET
              precedence = excluded.precedence,
              format = excluded.format,
              is_project_shared = excluded.is_project_shared
            """,
            (
                f"{OPENCODE_AGENT_ID}:path:control-plane",
                OPENCODE_AGENT_ID,
                config_record.path,
                config_record.precedence,
                config_record.format,
                int(config_record.is_project_shared),
            ),
        )


def _install_plan_id(agent_id: str, target_path: str, before_hash: str) -> str:
    digest = hashlib.sha256(f"{agent_id}\0{target_path}\0{before_hash}".encode()).hexdigest()
    return f"install_{digest[:24]}"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(target.parent),
        delete=False,
    ) as handle:
        handle.write(text)
        temp_name = handle.name
    Path(temp_name).replace(target)


__all__ = [
    "CODEX_CONTROL_TOKEN_ENV_VAR",
    "CLAUDE_CODE_AGENT_ID",
    "CLAUDE_CODE_CONTROL_HELPER",
    "CLINE_AGENT_ID",
    "CLINE_CONTROL_HELPER",
    "CODEX_AGENT_ID",
    "GEMINI_AGENT_ID",
    "OPENCODE_AGENT_ID",
    "ControlPlaneInstallError",
    "ControlPlaneInstallPreview",
    "MCP_HUB_URL",
    "install_claude_code_control_plane",
    "install_cline_control_plane",
    "install_codex_control_plane",
    "install_gemini_control_plane",
    "install_opencode_control_plane",
    "plan_claude_code_control_plane_install",
    "plan_cline_control_plane_install",
    "plan_codex_control_plane_install",
    "plan_gemini_control_plane_install",
    "plan_opencode_control_plane_install",
]
