"""Atomic config apply, backup, verification, and rollback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp_multiplex.adapters import (
    AgentRegistry,
    ClaudeCodeAdapterError,
    ClineAdapterError,
    CodexAdapterError,
    GeminiAdapterError,
    OpenCodeAdapterError,
    parse_claude_code_config,
    parse_cline_config,
    parse_codex_config,
    parse_gemini_config,
    parse_opencode_config,
)
from mcp_multiplex.approvals import ApprovalStore
from mcp_multiplex.observability import EventStore
from mcp_multiplex.storage import migrate


class ApplyError(ValueError):
    """Raised when an approved remediation plan cannot be applied safely."""


class RollbackError(ValueError):
    """Raised when a stored backup cannot be restored safely."""


PostWriteValidator = Callable[[Path], None]
AUTO_APPLY_REASON = "certified_safe_known_direct"


@dataclass(frozen=True)
class ApplyPlan:
    """Stored remediation plan fields needed by the atomic writer."""

    plan_id: str
    plan_type: str
    status: str
    agent_id: str | None
    target_path: str
    observed_entry_id: str
    policy: dict[str, Any]
    expected_preimage_hash: str

    @property
    def approval_required(self) -> bool:
        return self.policy.get("approval_required") is True


@dataclass(frozen=True)
class ConfigBackup:
    """Stored backup metadata plus exact backup path."""

    backup_id: str
    plan_id: str
    target_path: str
    backup_path: str
    before_hash: str
    bytes: int
    created_at: str
    restored_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "plan_id": self.plan_id,
            "target_path": self.target_path,
            "backup_path": self.backup_path,
            "before_hash": self.before_hash,
            "bytes": self.bytes,
            "created_at": self.created_at,
            "restored_at": self.restored_at,
        }


@dataclass(frozen=True)
class ApplyResult:
    """Successful atomic apply result."""

    plan_id: str
    target_path: str
    backup: ConfigBackup
    before_hash: str
    after_hash: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "target_path": self.target_path,
            "backup": self.backup.to_dict(),
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class RollbackResult:
    """Rollback result proving exact bytes were restored."""

    backup: ConfigBackup
    restored_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup": self.backup.to_dict(),
            "restored_hash": self.restored_hash,
        }


@dataclass(frozen=True)
class AutoApplyDecision:
    """Eligibility decision for certified safe automatic remediation."""

    plan_id: str
    eligible: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "eligible": self.eligible,
            "reasons": self.reasons,
        }


class ConfigBackupStore:
    """SQLite-backed config backup metadata repository."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate(connection)

    def create(
        self,
        *,
        plan_id: str,
        target_path: Path,
        content: bytes,
        backup_dir: Path | None = None,
        created_at: str | None = None,
    ) -> ConfigBackup:
        """Persist exact pre-image bytes and metadata before a config mutation."""
        timestamp = created_at or _current_timestamp()
        before_hash = sha256_bytes(content)
        backup_id = _backup_id(plan_id, before_hash, timestamp)
        directory = backup_dir or (target_path.parent / ".mcp-multiplex-backups")
        directory.mkdir(parents=True, exist_ok=True)
        backup_path = directory / f"{backup_id}.bak"
        backup_path.write_bytes(content)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO config_backups (
                  backup_id,
                  plan_id,
                  target_path,
                  backup_path,
                  before_hash,
                  bytes,
                  created_at,
                  restored_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    backup_id,
                    plan_id,
                    str(target_path),
                    str(backup_path),
                    before_hash,
                    len(content),
                    timestamp,
                ),
            )
        return self.show(backup_id)

    def show(self, backup_id: str) -> ConfigBackup:
        """Return one backup metadata record."""
        row = self.connection.execute(
            """
            SELECT *
            FROM config_backups
            WHERE backup_id = ?
            """,
            (backup_id,),
        ).fetchone()
        if row is None:
            raise KeyError(backup_id)
        return _backup_from_row(row)

    def list(self, *, plan_id: str | None = None) -> list[ConfigBackup]:
        """List backup records in deterministic order."""
        clauses: list[str] = []
        params: list[str] = []
        if plan_id is not None:
            clauses.append("plan_id = ?")
            params.append(plan_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT backup_id
            FROM config_backups
            {where}
            ORDER BY created_at, backup_id
            """,
            params,
        ).fetchall()
        return [self.show(str(row["backup_id"])) for row in rows]

    def mark_restored(self, backup_id: str, *, restored_at: str | None = None) -> ConfigBackup:
        """Mark a backup as restored."""
        timestamp = restored_at or _current_timestamp()
        with self.connection:
            self.connection.execute(
                """
                UPDATE config_backups
                SET restored_at = ?
                WHERE backup_id = ?
                """,
                (timestamp, backup_id),
            )
        return self.show(backup_id)


def evaluate_auto_apply(connection: sqlite3.Connection, plan_id: str) -> AutoApplyDecision:
    """Return whether a plan is eligible for certified safe auto-apply."""
    migrate(connection)
    plan = _load_apply_plan(connection, plan_id)
    reasons: list[str] = []
    if plan.plan_type != "rewrite_known_direct":
        reasons.append("plan_type_not_known_direct_rewrite")
    if plan.status != "pending_approval":
        reasons.append(f"plan_status_not_pending_approval:{plan.status}")
    if not plan.approval_required:
        reasons.append("plan_not_approval_gated")

    try:
        agent = AgentRegistry(connection).show(plan.agent_id or "")
    except KeyError:
        agent = None
        reasons.append("agent_not_registered")
    if agent is not None and not agent.can_auto_remediate:
        reasons.append(f"agent_not_certified:{agent.certification_level}")
    if agent is not None and not _target_path_is_registered_non_project_shared(
        agent.config_paths, plan.target_path
    ):
        reasons.append("target_path_missing_or_project_shared")

    policy_reasons = plan.policy.get("reasons", [])
    if not isinstance(policy_reasons, list):
        policy_reasons = []
    if "known_direct_backend_match" not in policy_reasons:
        reasons.append("policy_missing_known_direct_backend_match")
    if "match_confidence:high" not in policy_reasons:
        reasons.append("policy_match_confidence_not_high")
    if not any(reason == "match_reason:exact_backend_fingerprint" for reason in policy_reasons):
        reasons.append("policy_missing_exact_backend_fingerprint")

    before = _policy_before(plan)
    after = _policy_after(plan)
    if before.get("transport") != "stdio" or not before.get("command"):
        reasons.append("before_state_not_stdio_direct")
    if before.get("cwd") is not None:
        reasons.append("cwd_ambiguity")
    env_names = before.get("env_names", [])
    if not isinstance(env_names, list) or env_names:
        reasons.append("env_ambiguity")
    expected_url = after.get("url")
    if not isinstance(expected_url, str) or not expected_url.startswith("http://127.0.0.1:30000/"):
        reasons.append("after_state_not_local_hub_url")

    try:
        _verify_preimage(plan, Path(plan.target_path))
    except ApplyError as error:
        reasons.append(f"preimage_not_current:{error}")

    if ConfigBackupStore(connection).list(plan_id=plan.plan_id):
        reasons.append("rewrite_loop_guard_backup_exists")
    prior_apply_events = EventStore(connection).query(plan_id=plan.plan_id)
    if any(
        event.event.event_type
        in {"remediation.applied", "remediation.failed", "rollback.completed"}
        for event in prior_apply_events
    ):
        reasons.append("rewrite_loop_guard_prior_mutation_event")

    return AutoApplyDecision(
        plan_id=plan.plan_id,
        eligible=not reasons,
        reasons=reasons if reasons else [AUTO_APPLY_REASON],
    )


def auto_apply_plan(
    connection: sqlite3.Connection,
    plan_id: str,
    *,
    actor: str = "daemon:auto_apply",
    backup_dir: Path | None = None,
    timestamp: str | None = None,
    post_write_validator: PostWriteValidator | None = None,
) -> ApplyResult:
    """Authorize and apply a certified safe known-direct rewrite automatically."""
    decision = evaluate_auto_apply(connection, plan_id)
    if not decision.eligible:
        EventStore(connection).append(
            event_id=_event_id(plan_id, "auto_apply.rejected", timestamp),
            event_type="auto_apply.rejected",
            actor=actor,
            plan_id=plan_id,
            result="failure",
            payload=decision.to_dict(),
            timestamp=timestamp,
        )
        raise ApplyError(f"auto-apply rejected: {', '.join(decision.reasons)}")

    _authorize_auto_apply(connection, plan_id, decision, actor=actor, timestamp=timestamp)
    return apply_plan(
        connection,
        plan_id,
        actor=actor,
        backup_dir=backup_dir,
        timestamp=timestamp,
        post_write_validator=post_write_validator,
    )


def apply_plan(
    connection: sqlite3.Connection,
    plan_id: str,
    *,
    actor: str = "daemon",
    backup_dir: Path | None = None,
    timestamp: str | None = None,
    post_write_validator: PostWriteValidator | None = None,
) -> ApplyResult:
    """Apply an approved known-direct rewrite plan with backup and rollback."""
    migrate(connection)
    plan = _load_apply_plan(connection, plan_id)
    _require_apply_allowed(connection, plan)
    if plan.plan_type != "rewrite_known_direct":
        raise ApplyError(f"unsupported apply plan_type: {plan.plan_type}")
    target = Path(plan.target_path)
    before_bytes = _read_existing_file(target)
    before_hash = sha256_bytes(before_bytes)
    _verify_preimage(plan, target)
    backup = ConfigBackupStore(connection).create(
        plan_id=plan.plan_id,
        target_path=target,
        content=before_bytes,
        backup_dir=backup_dir,
        created_at=timestamp,
    )
    try:
        after_text = _rewrite_known_direct(before_bytes.decode("utf-8"), plan)
        _validate_rewritten_config(after_text, target, plan)
        _atomic_write_text(target, after_text)
        if post_write_validator is not None:
            post_write_validator(target)
        _verify_post_state(plan, target)
    except Exception as error:
        rollback = rollback_backup(
            connection,
            backup.backup_id,
            actor=actor,
            timestamp=timestamp,
            reason=str(error),
        )
        _mark_plan_failed(connection, plan.plan_id)
        EventStore(connection).append(
            event_id=_event_id(plan.plan_id, "remediation.failed", timestamp),
            event_type="remediation.failed",
            actor=actor,
            agent_id=plan.agent_id,
            plan_id=plan.plan_id,
            target_path=plan.target_path,
            before_hash=before_hash,
            after_hash=rollback.restored_hash,
            backup_id=backup.backup_id,
            result="failure",
            payload={"error": str(error), "rollback_complete": True},
            timestamp=timestamp,
        )
        raise ApplyError(f"apply failed and rollback completed: {error}") from error

    after_bytes = target.read_bytes()
    after_hash = sha256_bytes(after_bytes)
    with connection:
        connection.execute(
            """
            UPDATE remediation_plans
            SET status = 'applied',
                updated_at = CURRENT_TIMESTAMP
            WHERE plan_id = ?
            """,
            (plan.plan_id,),
        )
    EventStore(connection).append(
        event_id=_event_id(plan.plan_id, "remediation.applied", timestamp),
        event_type="remediation.applied",
        actor=actor,
        agent_id=plan.agent_id,
        plan_id=plan.plan_id,
        target_path=plan.target_path,
        before_hash=before_hash,
        after_hash=after_hash,
        backup_id=backup.backup_id,
        result="success",
        payload={"verified": True},
        timestamp=timestamp,
    )
    return ApplyResult(
        plan_id=plan.plan_id,
        target_path=plan.target_path,
        backup=backup,
        before_hash=before_hash,
        after_hash=after_hash,
        verified=True,
    )


def rollback_backup(
    connection: sqlite3.Connection,
    backup_id: str,
    *,
    actor: str = "daemon",
    timestamp: str | None = None,
    reason: str = "operator_requested",
) -> RollbackResult:
    """Restore exact bytes from a backup using an atomic replacement."""
    store = ConfigBackupStore(connection)
    backup = store.show(backup_id)
    target = Path(backup.target_path)
    backup_path = Path(backup.backup_path)
    if not backup_path.is_file():
        raise RollbackError(f"backup bytes missing: {backup.backup_path}")
    content = backup_path.read_bytes()
    restored_hash = sha256_bytes(content)
    if restored_hash != backup.before_hash:
        raise RollbackError("backup content hash does not match metadata")
    _atomic_write_bytes(target, content)
    restored = store.mark_restored(backup.backup_id, restored_at=timestamp)
    EventStore(connection).append(
        event_id=_event_id(backup.plan_id, f"rollback.{backup.backup_id}", timestamp),
        event_type="rollback.completed",
        actor=actor,
        plan_id=backup.plan_id,
        target_path=backup.target_path,
        before_hash=backup.before_hash,
        after_hash=restored_hash,
        backup_id=backup.backup_id,
        result="success",
        payload={"reason": reason, "restored_bytes": backup.bytes},
        timestamp=timestamp,
    )
    return RollbackResult(backup=restored, restored_hash=restored_hash)


def sha256_bytes(content: bytes) -> str:
    """Return a schema-compatible sha256 digest for bytes."""
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _load_apply_plan(connection: sqlite3.Connection, plan_id: str) -> ApplyPlan:
    row = connection.execute(
        """
        SELECT plan_id,
               plan_type,
               status,
               agent_id,
               target_path,
               observed_entry_id,
               policy_json,
               expected_preimage_hash
        FROM remediation_plans
        WHERE plan_id = ?
        """,
        (plan_id,),
    ).fetchone()
    if row is None:
        raise ApplyError(f"unknown plan: {plan_id}")
    target_path = row["target_path"]
    observed_entry_id = row["observed_entry_id"]
    expected_preimage_hash = row["expected_preimage_hash"]
    if not target_path or not observed_entry_id or not expected_preimage_hash:
        raise ApplyError("plan is missing target_path, observed_entry_id, or preimage hash")
    return ApplyPlan(
        plan_id=str(row["plan_id"]),
        plan_type=str(row["plan_type"]),
        status=str(row["status"]),
        agent_id=row["agent_id"],
        target_path=str(target_path),
        observed_entry_id=str(observed_entry_id),
        policy=json.loads(str(row["policy_json"])),
        expected_preimage_hash=str(expected_preimage_hash),
    )


def _require_apply_allowed(connection: sqlite3.Connection, plan: ApplyPlan) -> None:
    if plan.status == "rejected":
        raise ApplyError("rejected plan cannot apply")
    if plan.approval_required:
        approval = ApprovalStore(connection).find_by_plan(plan.plan_id)
        if approval is None or approval.state != "approved" or plan.status != "approved":
            raise ApplyError("approval required plans cannot apply without approval")
    elif plan.status not in {"draft", "approved"}:
        raise ApplyError(f"plan status cannot apply: {plan.status}")


def _authorize_auto_apply(
    connection: sqlite3.Connection,
    plan_id: str,
    decision: AutoApplyDecision,
    *,
    actor: str,
    timestamp: str | None,
) -> None:
    plan = _load_apply_plan(connection, plan_id)
    policy = dict(plan.policy)
    policy["approval_required"] = False
    policy["auto_apply_allowed"] = True
    policy["auto_apply_reason"] = AUTO_APPLY_REASON
    with connection:
        connection.execute(
            """
            UPDATE remediation_plans
            SET status = 'approved',
                policy_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE plan_id = ?
            """,
            (json.dumps(policy, sort_keys=True, separators=(",", ":")), plan_id),
        )
    ApprovalStore(connection).create_not_required(
        plan_id,
        actor=actor,
        channel="auto_policy",
        created_at=timestamp,
    )
    EventStore(connection).append(
        event_id=_event_id(plan_id, "auto_apply.authorized", timestamp),
        event_type="auto_apply.authorized",
        actor=actor,
        agent_id=plan.agent_id,
        plan_id=plan.plan_id,
        target_path=plan.target_path,
        result="success",
        payload=decision.to_dict(),
        timestamp=timestamp,
    )


def _target_path_is_registered_non_project_shared(
    config_paths: list[Any], target_path: str
) -> bool:
    return any(path.path == target_path and not path.is_project_shared for path in config_paths)


def _read_existing_file(target: Path) -> bytes:
    try:
        return target.read_bytes()
    except OSError as error:
        raise ApplyError(f"cannot read target config: {target}: {error}") from error


def _verify_preimage(plan: ApplyPlan, target: Path) -> None:
    parsed = _parse_config_for_plan(target, plan, phase="pre-image")
    for entry in parsed.observed_entries:
        if entry.observed_entry_id == plan.observed_entry_id:
            if entry.entry_hash != plan.expected_preimage_hash:
                raise ApplyError("expected pre-image hash does not match observed entry")
            return
    raise ApplyError("expected observed entry is missing from pre-image")


def _verify_post_state(plan: ApplyPlan, target: Path) -> None:
    after = _policy_after(plan)
    expected_url = after.get("url")
    if not isinstance(expected_url, str) or not expected_url:
        raise ApplyError("plan policy is missing expected post-state url")
    parsed = _parse_config_for_plan(target, plan, phase="post-image")
    for entry in parsed.observed_entries:
        if entry.mount_name == after.get("mount_name"):
            if entry.transport != "streamable_http" or entry.url != expected_url:
                raise ApplyError("post-write verification failed: entry is not Hub-routed")
            if entry.command is not None or entry.args:
                raise ApplyError("post-write verification failed: direct command remains")
            return
    raise ApplyError("post-write verification failed: target entry missing")


def _parse_config_for_plan(target: Path, plan: ApplyPlan, *, phase: str) -> Any:
    agent_id = plan.agent_id or ""
    if agent_id.startswith("agent_claude_"):
        try:
            return parse_claude_code_config(
                target,
                agent_id=agent_id or "agent_claude_user_default",
            )
        except ClaudeCodeAdapterError as error:
            raise ApplyError(f"{phase} parse failed: {error}") from error
    if agent_id.startswith("agent_gemini_"):
        try:
            return parse_gemini_config(target, agent_id=agent_id or "agent_gemini_user_default")
        except GeminiAdapterError as error:
            raise ApplyError(f"{phase} parse failed: {error}") from error
    if agent_id.startswith("agent_cline_"):
        try:
            return parse_cline_config(target, agent_id=agent_id or "agent_cline_user_default")
        except ClineAdapterError as error:
            raise ApplyError(f"{phase} parse failed: {error}") from error
    if agent_id.startswith("agent_opencode_"):
        try:
            return parse_opencode_config(target, agent_id=agent_id or "agent_opencode_user_default")
        except OpenCodeAdapterError as error:
            raise ApplyError(f"{phase} parse failed: {error}") from error
    try:
        return parse_codex_config(target, agent_id=agent_id or "agent_codex_user_default")
    except CodexAdapterError as error:
        raise ApplyError(f"{phase} parse failed: {error}") from error


def _rewrite_known_direct(text: str, plan: ApplyPlan) -> str:
    if (plan.agent_id or "").startswith("agent_claude_"):
        return _rewrite_json_mcp_servers_known_direct(text, plan)
    if (plan.agent_id or "").startswith("agent_gemini_"):
        return _rewrite_json_mcp_servers_known_direct(text, plan, url_key="httpUrl")
    if (plan.agent_id or "").startswith("agent_cline_"):
        return _rewrite_json_mcp_servers_known_direct(text, plan, cline_transport=True)
    if (plan.agent_id or "").startswith("agent_opencode_"):
        return _rewrite_opencode_known_direct(text, plan)
    return _rewrite_codex_known_direct(text, plan)


def _rewrite_codex_known_direct(text: str, plan: ApplyPlan) -> str:
    before = _policy_before(plan)
    after = _policy_after(plan)
    mount_name = before.get("mount_name")
    expected_url = after.get("url")
    if not isinstance(mount_name, str) or not mount_name:
        raise ApplyError("plan policy is missing mount_name")
    if not isinstance(expected_url, str) or not expected_url:
        raise ApplyError("plan policy is missing target Hub url")
    parsed = tomllib.loads(text)
    servers = parsed.get("mcp_servers", {})
    if not isinstance(servers, dict) or mount_name not in servers:
        raise ApplyError(f"target MCP entry not found: {mount_name}")
    raw_entry = servers[mount_name]
    if not isinstance(raw_entry, dict):
        raise ApplyError(f"target MCP entry has invalid shape: {mount_name}")
    replacement_entry = _replacement_entry(raw_entry, expected_url)
    replacement_block = _toml_block(["mcp_servers", mount_name], replacement_entry)
    pattern = re.compile(rf"(?ms)^\[mcp_servers\.{re.escape(mount_name)}\]\n.*?(?=^\[|\Z)")
    rewritten, count = pattern.subn(replacement_block, text, count=1)
    if count != 1:
        raise ApplyError(f"could not rewrite MCP entry block: {mount_name}")
    return rewritten if rewritten.endswith("\n") else f"{rewritten}\n"


def _rewrite_json_mcp_servers_known_direct(
    text: str,
    plan: ApplyPlan,
    *,
    url_key: str = "url",
    cline_transport: bool = False,
) -> str:
    before = _policy_before(plan)
    after = _policy_after(plan)
    mount_name = before.get("mount_name")
    expected_url = after.get("url")
    if not isinstance(mount_name, str) or not mount_name:
        raise ApplyError("plan policy is missing mount_name")
    if not isinstance(expected_url, str) or not expected_url:
        raise ApplyError("plan policy is missing target Hub url")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ApplyError(f"invalid JSON target config: {error}") from error
    if not isinstance(parsed, dict):
        raise ApplyError("JSON target config root must be an object")
    servers = parsed.get("mcpServers")
    if not isinstance(servers, dict) or mount_name not in servers:
        raise ApplyError(f"target MCP entry not found: {mount_name}")
    raw_entry = servers[mount_name]
    if not isinstance(raw_entry, dict):
        raise ApplyError(f"target MCP entry has invalid shape: {mount_name}")
    replacement_entry = (
        _cline_replacement_entry(raw_entry, expected_url)
        if cline_transport
        else _replacement_entry(raw_entry, expected_url, url_key=url_key)
    )
    if not cline_transport:
        replacement_entry["type"] = "http"
    servers[mount_name] = replacement_entry
    return json.dumps(parsed, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _replacement_entry(
    raw_entry: dict[str, Any],
    expected_url: str,
    *,
    url_key: str = "url",
) -> dict[str, Any]:
    keep_keys = (
        "enabled",
        "disabled",
        "enabled_tools",
        "disabled_tools",
        "approval_policy",
        "trust",
    )
    replacement: dict[str, Any] = {url_key: expected_url}
    for key in keep_keys:
        if key in raw_entry:
            replacement[key] = raw_entry[key]
    return replacement


def _cline_replacement_entry(raw_entry: dict[str, Any], expected_url: str) -> dict[str, Any]:
    keep_keys = (
        "enabled",
        "disabled",
        "enabled_tools",
        "disabled_tools",
        "approval_policy",
        "autoApprove",
        "alwaysAllow",
    )
    replacement: dict[str, Any] = {"transport": {"type": "streamableHttp", "url": expected_url}}
    for key in keep_keys:
        if key in raw_entry:
            replacement[key] = raw_entry[key]
    return replacement


def _rewrite_opencode_known_direct(text: str, plan: ApplyPlan) -> str:
    before = _policy_before(plan)
    after = _policy_after(plan)
    mount_name = before.get("mount_name")
    expected_url = after.get("url")
    if not isinstance(mount_name, str) or not mount_name:
        raise ApplyError("plan policy is missing mount_name")
    if not isinstance(expected_url, str) or not expected_url:
        raise ApplyError("plan policy is missing target Hub url")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ApplyError(f"invalid JSON target config: {error}") from error
    if not isinstance(parsed, dict):
        raise ApplyError("OpenCode target config root must be an object")
    container_key = "mcp" if "mcp" in parsed else "mcpServers"
    servers = parsed.get(container_key)
    if not isinstance(servers, dict) or mount_name not in servers:
        raise ApplyError(f"target MCP entry not found: {mount_name}")
    raw_entry = servers[mount_name]
    if not isinstance(raw_entry, dict):
        raise ApplyError(f"target MCP entry has invalid shape: {mount_name}")
    if container_key == "mcp":
        replacement_entry: dict[str, Any] = {"type": "remote", "url": expected_url}
        if "enabled" in raw_entry:
            replacement_entry["enabled"] = raw_entry["enabled"]
        servers[mount_name] = replacement_entry
        return json.dumps(parsed, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    replacement_entry = _replacement_entry(raw_entry, expected_url)
    replacement_entry["type"] = "http"
    servers[mount_name] = replacement_entry
    return json.dumps(parsed, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _toml_block(path: list[str], values: dict[str, Any]) -> str:
    lines = [f"[{'.'.join(path)}]"]
    for key, value in values.items():
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[" + ", ".join(json.dumps(item) for item in value) + "]"
    raise ApplyError(f"unsupported TOML value in preserved field: {value!r}")


def _validate_codex_toml(text: str, target: Path) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ApplyError(f"syntax validation failed for {target}: {error}") from error


def _validate_rewritten_config(text: str, target: Path, plan: ApplyPlan) -> None:
    if (plan.agent_id or "").startswith(
        ("agent_claude_", "agent_gemini_", "agent_cline_", "agent_opencode_")
    ):
        try:
            json.loads(text)
        except json.JSONDecodeError as error:
            raise ApplyError(f"syntax validation failed for {target}: {error}") from error
        return
    _validate_codex_toml(text, target)


def _policy_before(plan: ApplyPlan) -> dict[str, Any]:
    before = plan.policy.get("before")
    if not isinstance(before, dict):
        raise ApplyError("plan policy is missing before projection")
    return before


def _policy_after(plan: ApplyPlan) -> dict[str, Any]:
    after = plan.policy.get("after")
    if not isinstance(after, dict):
        raise ApplyError("plan policy is missing after projection")
    return after


def _atomic_write_text(target: Path, text: str) -> None:
    _atomic_write_bytes(target, text.encode("utf-8"))


def _atomic_write_bytes(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    original_mode = target.stat().st_mode if target.exists() else None
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if original_mode is not None:
            os.chmod(tmp_path, original_mode)
        os.replace(tmp_path, target)
        _fsync_directory(target.parent)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            raise


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mark_plan_failed(connection: sqlite3.Connection, plan_id: str) -> None:
    with connection:
        connection.execute(
            """
            UPDATE remediation_plans
            SET status = 'failed',
                updated_at = CURRENT_TIMESTAMP
            WHERE plan_id = ?
            """,
            (plan_id,),
        )


def _backup_id(plan_id: str, before_hash: str, timestamp: str) -> str:
    digest = hashlib.sha256(f"{plan_id}\0{before_hash}\0{timestamp}".encode()).hexdigest()
    return f"bak_{digest[:24]}"


def _backup_from_row(row: sqlite3.Row) -> ConfigBackup:
    return ConfigBackup(
        backup_id=str(row["backup_id"]),
        plan_id=str(row["plan_id"]),
        target_path=str(row["target_path"]),
        backup_path=str(row["backup_path"]),
        before_hash=str(row["before_hash"]),
        bytes=int(row["bytes"]),
        created_at=str(row["created_at"]),
        restored_at=row["restored_at"],
    )


def _event_id(plan_id: str, event_type: str, timestamp: str | None) -> str:
    event_time = timestamp or _current_timestamp()
    digest = hashlib.sha256(f"{plan_id}\0{event_type}\0{event_time}".encode()).hexdigest()
    return f"evt_{digest[:24]}"


def _current_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "AUTO_APPLY_REASON",
    "AutoApplyDecision",
    "ApplyError",
    "ApplyPlan",
    "ApplyResult",
    "ConfigBackup",
    "ConfigBackupStore",
    "RollbackError",
    "RollbackResult",
    "apply_plan",
    "auto_apply_plan",
    "evaluate_auto_apply",
    "rollback_backup",
    "sha256_bytes",
]
