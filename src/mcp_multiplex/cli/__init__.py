"""CLI entrypoint for operator commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO
from urllib.error import URLError
from urllib.request import Request, urlopen

from mcp_multiplex import __version__
from mcp_multiplex.adapters import AgentRegistry, discover_config_paths
from mcp_multiplex.apply import ApplyError, RollbackError, apply_plan, rollback_backup
from mcp_multiplex.approvals import ApprovalError, ApprovalStore
from mcp_multiplex.catalog import (
    CatalogCandidateStore,
    CatalogStore,
    LegacyCatalogImportError,
    apply_legacy_mcp_hub_catalog_import,
    plan_legacy_mcp_hub_catalog_import,
    validate_routable_catalog_entry,
)
from mcp_multiplex.config import ConfigLoadError, inspect_config, resolve_environment_layout
from mcp_multiplex.credentials import CredentialRefStore, readiness_summary
from mcp_multiplex.daemon import DEFAULT_HOST, DEFAULT_PORT
from mcp_multiplex.health import daemon_unavailable_payload, is_health_payload
from mcp_multiplex.install import (
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
from mcp_multiplex.observability import (
    EventStore,
    ObservedEntryStore,
    WatchedConfigPath,
    classify_observed_entries,
    health_payload_for_classifications,
    parse_watched_config,
    run_config_audit,
)
from mcp_multiplex.planning import plan_self_healing_dry_run
from mcp_multiplex.runtime import RuntimeBackendStore, RuntimeFrontendSessionStore
from mcp_multiplex.schemas import CatalogEntry, RemediationPlan
from mcp_multiplex.security import HUB_ORIGIN
from mcp_multiplex.service import (
    UserServiceInstallError,
    install_user_service,
    plan_user_service_install,
    user_service_status,
)
from mcp_multiplex.storage import connect

DEFAULT_AGENT_IDS = {
    "codex": "agent_codex_user_default",
    "claude_code": "agent_claude_code_user_default",
    "gemini": "agent_gemini_user_default",
    "cline": "agent_cline_user_default",
    "opencode": "agent_opencode_user_default",
}

DEFAULT_AGENT_DISPLAY_NAMES = {
    "codex": "Codex CLI",
    "claude_code": "Claude Code",
    "gemini": "Gemini CLI",
    "cline": "Cline",
    "opencode": "OpenCode",
}

LEGACY_DISCOVERY_ROOTS = (
    "~/mcp-hub",
    "~/attic",
    "/home/core/dev/attic",
    "/home/core/dev/backups",
)


def daemon_health_url(host: str, port: int) -> str:
    """Build the local daemon health URL."""
    return f"http://{host}:{port}/healthz"


def fetch_health(host: str, port: int, timeout: float) -> tuple[int, object]:
    """Fetch daemon health from the local daemon."""
    request = Request(daemon_health_url(host, port), headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        body = response.read()
        status = response.status
    return status, json.loads(body.decode("utf-8"))


def health_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Print daemon health as JSON."""
    try:
        status, payload = fetch_health(args.host, args.port, args.timeout)
    except (OSError, TimeoutError, URLError) as error:
        payload = daemon_unavailable_payload(str(error))
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2

    if status != 200 or not is_health_payload(payload):
        payload = daemon_unavailable_payload(f"invalid health response: status={status}")
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2

    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    if isinstance(payload, dict) and payload.get("ok") is True:
        return 0
    return 1


def status_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Print compact or JSON status from local Multiplex state."""
    connection = _state_connection(args)
    payload = status_payload(connection)
    _augment_status_with_onboarding(
        payload,
        connection=connection,
        home=Path(args.home).expanduser() if args.home else None,
    )
    if args.compact:
        stdout.write(compact_status(payload) + "\n")
    else:
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] is True else 1


def audit_run_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Run an observe-only config audit over registered config paths."""
    connection = _state_connection(args)
    targets = _registered_watch_targets(connection)
    result = run_config_audit(
        connection,
        targets,
        trigger="startup",
        run_id=args.run_id or "cli_audit_run",
        actor=args.actor,
    )
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexAuditRun",
        "target_count": len(targets),
        "targets": [str(target.path) for target in targets],
        "result": result.to_dict(),
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if result.health["ok"] else 1


def audit_plan_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Run dry-run remediation planning over registered config paths and store plans."""
    connection = _state_connection(args)
    targets = _registered_watch_targets(connection)
    observed_entries = []
    for target in sorted(targets, key=lambda item: (str(item.path), item.agent_id)):
        observed_entries.extend(parse_watched_config(target))
    result = plan_self_healing_dry_run(
        connection,
        observed_entries,
        actor=args.actor,
        run_id=args.run_id,
        include_missing_control_plane=not args.skip_missing_control_plane,
    )
    inserted = []
    existing = []
    for plan in result.plans:
        if _insert_remediation_plan(connection, plan):
            inserted.append(plan.plan_id)
        else:
            existing.append(plan.plan_id)
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexAuditPlan",
        "ok": True,
        "target_count": len(targets),
        "targets": [str(target.path) for target in targets],
        "plan_count": len(result.plans),
        "inserted_count": len(inserted),
        "existing_count": len(existing),
        "inserted_plan_ids": inserted,
        "existing_plan_ids": existing,
        "result": result.to_dict(),
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def plan_list_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """List remediation plans."""
    connection = _state_connection(args)
    plans = _plan_rows(connection, status=args.status)
    payload = {"schema_version": 1, "kind": "MCPMultiplexPlanList", "plans": plans}
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def plan_show_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Show one remediation plan."""
    connection = _state_connection(args)
    try:
        plan = _plan_row(connection, args.plan_id)
    except KeyError:
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexPlanShow",
                    "ok": False,
                    "error": {"detail": f"unknown plan: {args.plan_id}"},
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    stdout.write(
        json.dumps(
            {"schema_version": 1, "kind": "MCPMultiplexPlanShow", "ok": True, "plan": plan},
            sort_keys=True,
        )
        + "\n"
    )
    return 0


def apply_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Apply an approved remediation plan."""
    connection = _state_connection(args)
    try:
        result = apply_plan(
            connection,
            args.plan_id,
            actor=args.actor,
            backup_dir=Path(args.backup_dir).expanduser() if args.backup_dir else None,
        )
    except (ApplyError, KeyError) as error:
        payload = {
            "schema_version": 1,
            "kind": "MCPMultiplexApply",
            "ok": False,
            "error": {"detail": str(error)},
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexApply",
        "ok": True,
        "result": result.to_dict(),
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def rollback_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Rollback a config backup by id."""
    connection = _state_connection(args)
    try:
        result = rollback_backup(connection, args.backup_id, actor=args.actor)
    except (RollbackError, KeyError) as error:
        payload = {
            "schema_version": 1,
            "kind": "MCPMultiplexRollback",
            "ok": False,
            "error": {"detail": str(error)},
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexRollback",
        "ok": True,
        "result": result.to_dict(),
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def catalog_list_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """List catalog entries."""
    connection = _state_connection(args)
    entries = [entry.to_dict() for entry in CatalogStore(connection).list()]
    payload = {"schema_version": 1, "kind": "MCPMultiplexCatalogList", "entries": entries}
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def catalog_candidates_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """List staged catalog candidates."""
    connection = _state_connection(args)
    candidates = [candidate.to_dict() for candidate in CatalogCandidateStore(connection).list()]
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexCatalogCandidates",
        "candidates": candidates,
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def catalog_review_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Update catalog review/lifecycle state after operator review."""
    connection = _state_connection(args)
    store = CatalogStore(connection)
    try:
        before, after = store.set_review_state(
            args.catalog_id,
            review_state=args.review_state,
            lifecycle_state=args.lifecycle_state,
        )
    except (KeyError, ValueError) as error:
        payload = {
            "schema_version": 1,
            "kind": "MCPMultiplexCatalogReview",
            "ok": False,
            "error": {"detail": str(error)},
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2
    routability = validate_routable_catalog_entry(after)
    EventStore(connection).append(
        event_id=_catalog_review_event_id(
            after.catalog_id,
            before.review_state,
            after.review_state,
        ),
        event_type="catalog.reviewed",
        actor=args.actor,
        result="success",
        target_path=after.catalog_id,
        before_hash=_hash_json(before.to_dict()),
        after_hash=_hash_json(after.to_dict()),
        payload={
            "catalog_id": after.catalog_id,
            "review_state_before": before.review_state,
            "review_state_after": after.review_state,
            "lifecycle_state_before": before.lifecycle_state,
            "lifecycle_state_after": after.lifecycle_state,
            "routable": routability.routable,
            "routability_reasons": routability.reasons,
            "comment": args.comment,
        },
    )
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexCatalogReview",
        "ok": True,
        "entry": after.to_dict(),
        "routability": {
            "catalog_id": routability.catalog_id,
            "routable": routability.routable,
            "reasons": routability.reasons,
        },
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def catalog_review_legacy_import_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Bulk-review legacy MCP Hub catalog entries after operator inspection."""
    connection = _state_connection(args)
    store = CatalogStore(connection)
    entries = [
        entry
        for entry in store.list()
        if any(provenance.get("source") == "legacy_mcp_hub" for provenance in entry.provenance)
    ]
    results: list[dict[str, Any]] = []
    for entry in entries:
        before = entry
        if args.apply:
            before, after = store.set_review_state(
                entry.catalog_id,
                review_state=args.review_state,
                lifecycle_state=args.lifecycle_state,
            )
            routability = validate_routable_catalog_entry(after)
            EventStore(connection).append(
                event_id=_catalog_review_event_id(
                    after.catalog_id,
                    before.review_state,
                    after.review_state,
                ),
                event_type="catalog.reviewed",
                actor=args.actor,
                result="success",
                target_path=after.catalog_id,
                before_hash=_hash_json(before.to_dict()),
                after_hash=_hash_json(after.to_dict()),
                payload={
                    "catalog_id": after.catalog_id,
                    "review_scope": "legacy_mcp_hub",
                    "review_state_before": before.review_state,
                    "review_state_after": after.review_state,
                    "lifecycle_state_before": before.lifecycle_state,
                    "lifecycle_state_after": after.lifecycle_state,
                    "routable": routability.routable,
                    "routability_reasons": routability.reasons,
                    "comment": args.comment,
                },
            )
        else:
            after_payload = before.to_dict()
            after_payload["review_state"] = args.review_state
            after_payload["lifecycle_state"] = args.lifecycle_state
            after = CatalogEntry.from_dict(after_payload)
            routability = validate_routable_catalog_entry(after)
        results.append(
            {
                "catalog_id": after.catalog_id,
                "name": after.name,
                "review_state_before": before.review_state,
                "review_state_after": after.review_state,
                "lifecycle_state_before": before.lifecycle_state,
                "lifecycle_state_after": after.lifecycle_state,
                "would_change": before.to_dict() != after.to_dict(),
                "routable_after": routability.routable,
                "routability_reasons": routability.reasons,
            }
        )
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexLegacyCatalogReview",
        "ok": True,
        "mode": "apply" if args.apply else "dry_run",
        "review_scope": "legacy_mcp_hub",
        "entry_count": len(results),
        "changed_count": sum(1 for result in results if result["would_change"]),
        "review_state": args.review_state,
        "lifecycle_state": args.lifecycle_state,
        "results": results,
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def runtime_ps_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """List runtime backend and frontend sessions."""
    connection = _state_connection(args)
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexRuntimePs",
        "backends": [backend.to_dict() for backend in RuntimeBackendStore(connection).list()],
        "frontend_sessions": [
            session.to_dict() for session in RuntimeFrontendSessionStore(connection).list()
        ],
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def runtime_why_slow_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Explain runtime sharing/isolation state for one server or all servers."""
    connection = _state_connection(args)
    backends = RuntimeBackendStore(connection).list()
    if args.server:
        rows = connection.execute(
            "SELECT catalog_id FROM catalog_entries WHERE name = ?", (args.server,)
        ).fetchall()
        catalog_ids = {str(row["catalog_id"]) for row in rows}
        backends = [backend for backend in backends if backend.catalog_id in catalog_ids]
    diagnostics = []
    for backend in backends:
        diagnostics.append(
            {
                **backend.to_dict(),
                "sharing_explanation": _runtime_sharing_explanation(backend.runtime_pool_key),
            }
        )
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexRuntimeWhySlow",
        "server": args.server,
        "diagnostics": diagnostics,
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def agents_install_control_plane_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Install or preview the authenticated `mcp_hub` control-plane entry."""
    if args.agent not in {"codex", "claude_code", "cline", "gemini", "opencode"}:
        capability = control_plane_auth_capability(args.agent)
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexControlPlaneInstall",
                    "ok": False,
                    "agent": args.agent,
                    "capability": capability.to_dict(),
                    "error": {
                        "detail": (
                            f"agent {args.agent!r} automatic control-plane install "
                            f"is {capability.status}"
                        )
                    },
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    home = Path(args.home).expanduser() if args.home else None
    config_path = Path(args.config_path).expanduser() if args.config_path else None
    helper_path = Path(args.helper_path).expanduser() if args.helper_path else None
    try:
        if args.agent == "cline" and args.apply:
            result = install_cline_control_plane(
                _state_connection(args),
                home=home,
                config_path=config_path,
                helper_path=helper_path,
                backup_dir=Path(args.backup_dir).expanduser() if args.backup_dir else None,
                actor=args.actor,
            )
        elif args.agent == "cline":
            result = plan_cline_control_plane_install(
                home=home,
                config_path=config_path,
                helper_path=helper_path,
            )
        elif args.agent == "gemini" and args.apply:
            result = install_gemini_control_plane(
                _state_connection(args),
                home=home,
                config_path=config_path,
                backup_dir=Path(args.backup_dir).expanduser() if args.backup_dir else None,
                actor=args.actor,
            )
        elif args.agent == "gemini":
            result = plan_gemini_control_plane_install(
                home=home,
                config_path=config_path,
            )
        elif args.agent == "opencode" and args.apply:
            result = install_opencode_control_plane(
                _state_connection(args),
                home=home,
                config_path=config_path,
                backup_dir=Path(args.backup_dir).expanduser() if args.backup_dir else None,
                actor=args.actor,
            )
        elif args.agent == "opencode":
            result = plan_opencode_control_plane_install(
                home=home,
                config_path=config_path,
            )
        elif args.agent == "claude_code" and args.apply:
            result = install_claude_code_control_plane(
                _state_connection(args),
                home=home,
                config_path=config_path,
                helper_path=helper_path,
                backup_dir=Path(args.backup_dir).expanduser() if args.backup_dir else None,
                actor=args.actor,
            )
        elif args.agent == "claude_code":
            result = plan_claude_code_control_plane_install(
                home=home,
                config_path=config_path,
                helper_path=helper_path,
            )
        elif args.apply:
            result = install_codex_control_plane(
                _state_connection(args),
                home=home,
                config_path=config_path,
                backup_dir=Path(args.backup_dir).expanduser() if args.backup_dir else None,
                actor=args.actor,
            )
        else:
            result = plan_codex_control_plane_install(home=home, config_path=config_path)
    except ControlPlaneInstallError as error:
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexControlPlaneInstall",
                    "ok": False,
                    "error": {"detail": str(error)},
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexControlPlaneInstall",
        "ok": True,
        "mode": "apply" if args.apply else "dry_run",
        "result": result.to_dict(include_token=bool(args.emit_token)),
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    if args.apply and not args.emit_token:
        stderr.write(
            "Control token was issued and redacted. Re-run with --emit-token only in a "
            "controlled shell if you need one-time token output.\n"
        )
    return 0


def agents_auth_capabilities_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """List first-wave agent auth install capabilities."""
    capabilities = control_plane_auth_capabilities()
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexControlPlaneAuthCapabilities",
        "capabilities": [capability.to_dict() for capability in capabilities],
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def agents_self_check_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Return agent-facing mcp_hub readiness as stable JSON."""
    from mcp_multiplex.tui import tui_snapshot

    connection = _state_connection(args)
    home = Path(args.home).expanduser() if args.home else None
    agents = tui_snapshot(connection, home=home)["agents"]
    if args.agent:
        agents = [agent for agent in agents if agent["agent_kind"] == args.agent]
    not_ready = [agent for agent in agents if agent["self_check"] != "ready"]
    onboarding = _agent_onboarding_payload(
        connection,
        home=home,
        agent_kinds=[args.agent] if args.agent else None,
    )
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexAgentSelfCheck",
        "ok": not not_ready and onboarding["unregistered_count"] == 0 and bool(agents),
        "agent": args.agent,
        "agents": agents,
        "not_ready_count": len(not_ready),
        "discovered_config_paths": onboarding["discovered_config_paths"],
        "unregistered_agents": onboarding["unregistered_agents"],
        "next_action": onboarding["next_action"],
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] else 1


def agents_sync_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Discover first-wave agent configs and sync them into registry state."""
    connection = _state_connection(args)
    home = Path(args.home).expanduser() if args.home else None
    requested = [args.agent] if args.agent else None
    discovery = discover_config_paths(home=home, agent_kinds=requested)
    registry = AgentRegistry(connection)
    existing_by_kind = {agent.agent_kind: agent for agent in registry.list()}
    grouped: dict[str, list[Any]] = {}
    for config_path in discovery.config_paths:
        grouped.setdefault(config_path.agent_kind, []).append(config_path)
    changes: list[dict[str, Any]] = []
    synced_agents: list[dict[str, Any]] = []
    for agent_kind, discovered_paths in sorted(grouped.items()):
        existing = existing_by_kind.get(agent_kind)
        new_paths = [path.to_agent_config_path() for path in discovered_paths]
        before_paths = [] if existing is None else [
            {
                "path": config_path.path,
                "format": config_path.format,
                "precedence": config_path.precedence,
                "is_project_shared": config_path.is_project_shared,
            }
            for config_path in existing.config_paths
        ]
        after_paths = [
            {
                "path": config_path.path,
                "format": config_path.format,
                "precedence": config_path.precedence,
                "is_project_shared": config_path.is_project_shared,
            }
            for config_path in new_paths
        ]
        action = "create"
        if existing is not None and before_paths == after_paths:
            action = "unchanged"
        elif existing is not None:
            action = "update"
        changes.append(
            {
                "agent_kind": agent_kind,
                "agent_id": (
                    existing.agent_id if existing is not None else DEFAULT_AGENT_IDS[agent_kind]
                ),
                "action": action,
                "paths": after_paths,
            }
        )
        if args.apply and action != "unchanged":
            synced = registry.upsert(
                agent_id=(
                    existing.agent_id if existing is not None else DEFAULT_AGENT_IDS[agent_kind]
                ),
                agent_kind=agent_kind,
                display_name=(
                    existing.display_name
                    if existing is not None
                    else DEFAULT_AGENT_DISPLAY_NAMES[agent_kind]
                ),
                workspace_root=None if existing is None else existing.workspace_root,
                config_paths=new_paths,
                auth_token_ref=None if existing is None else existing.auth_token_ref,
                certification_level=(
                    "unverified" if existing is None else existing.certification_level
                ),
            )
            synced_agents.append(
                {
                    "agent_id": synced.agent_id,
                    "agent_kind": synced.agent_kind,
                    "config_path_count": len(synced.config_paths),
                }
            )
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexAgentSync",
        "ok": True,
        "mode": "apply" if args.apply else "dry_run",
        "discovery": discovery.to_dict(),
        "change_count": len([change for change in changes if change["action"] != "unchanged"]),
        "changes": changes,
        "synced_agents": synced_agents,
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def daemon_install_user_service_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Install or preview the daemon systemd user service."""
    home = Path(args.home).expanduser() if args.home else None
    unit_dir = Path(args.unit_dir).expanduser() if args.unit_dir else None
    db_path = Path(args.db_path).expanduser() if args.db_path else None
    try:
        if args.apply:
            result = install_user_service(
                _state_connection(args),
                home=home,
                unit_dir=unit_dir,
                db_path=db_path,
                daemon_bin=args.daemon_bin,
                host=args.host,
                port=args.port,
                actor=args.actor,
            )
        else:
            result = plan_user_service_install(
                home=home,
                unit_dir=unit_dir,
                db_path=db_path,
                daemon_bin=args.daemon_bin,
                host=args.host,
                port=args.port,
            )
    except UserServiceInstallError as error:
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexDaemonUserServiceInstall",
                    "ok": False,
                    "error": {"detail": str(error)},
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexDaemonUserServiceInstall",
        "ok": True,
        "mode": "apply" if args.apply else "dry_run",
        "result": result.to_dict(),
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def daemon_status_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Print daemon user-service status without mutating service state."""
    home = Path(args.home).expanduser() if args.home else None
    unit_dir = Path(args.unit_dir).expanduser() if args.unit_dir else None
    result = user_service_status(
        home=home,
        unit_dir=unit_dir,
        systemctl_bin=args.systemctl_bin,
        include_systemctl=not args.no_systemctl,
    )
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexDaemonUserServiceStatus",
        "ok": result.unit_exists and (result.systemctl_ok or not result.systemctl_available),
        "result": result.to_dict(),
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] is True else 1


def doctor_release_gate_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Run machine-level release-gate checks."""
    connection = _state_connection(args)
    payload = _release_gate_payload(
        connection,
        checkpoint_dir=Path(args.checkpoint_dir).expanduser(),
        global_cutover=bool(args.global_cutover),
        home=Path(args.home).expanduser() if args.home else None,
        daemon_unit_dir=Path(args.daemon_unit_dir).expanduser() if args.daemon_unit_dir else None,
        daemon_systemctl_bin=args.daemon_systemctl_bin,
    )
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] else 1


def doctor_retirement_gate_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the final read-only MCP Hub retirement completion gate."""
    connection = _state_connection(args)
    home = Path(args.home).expanduser() if args.home else None
    release_gate = _release_gate_payload(
        connection,
        checkpoint_dir=Path(args.checkpoint_dir).expanduser(),
        global_cutover=True,
        home=home,
        daemon_unit_dir=Path(args.daemon_unit_dir).expanduser() if args.daemon_unit_dir else None,
        daemon_systemctl_bin=args.daemon_systemctl_bin,
    )
    cutover_event = _latest_cutover_event(connection, "mcp-hub")
    footprint = _legacy_mcp_hub_footprint_payload(
        legacy_root=Path(args.legacy_root).expanduser().resolve(),
        home=home,
        ps_bin=args.ps_bin,
        include_processes=not args.no_processes,
    )
    cleanup_plan = _legacy_cleanup_plan_payload(footprint)
    checks = [
        {
            "name": "global_release_gate",
            "ok": release_gate["ok"],
            "release_gate": release_gate,
        },
        {
            "name": "cutover_applied",
            "ok": cutover_event is not None,
            "event": cutover_event.to_dict() if cutover_event is not None else None,
        },
        {
            "name": "legacy_footprint_clean",
            "ok": cleanup_plan["ok"],
            "cleanup_plan": cleanup_plan,
        },
    ]
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexRetirementGate",
        "ok": all(check["ok"] for check in checks),
        "source": "mcp-hub",
        "mode": "retirement_complete",
        "checks": checks,
        "unmanaged_process_action": "none",
        "mutation_action": "none",
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] else 1


def _release_gate_payload(
    connection: sqlite3.Connection,
    *,
    checkpoint_dir: Path,
    global_cutover: bool,
    home: Path | None,
    daemon_unit_dir: Path | None,
    daemon_systemctl_bin: str,
) -> dict[str, Any]:
    """Build the local or global release gate payload."""
    status = status_payload(connection)
    tamper_findings = [finding.__dict__ for finding in EventStore(connection).validate_hash_chain()]
    active_direct_bypasses = [
        blocker for blocker in status["blockers"] if blocker.get("code") == "active_direct_bypass"
    ]
    certification_check = _release_gate_certification_check(checkpoint_dir)
    mcp_hub_auth_findings = _release_gate_mcp_hub_auth_findings(connection)
    secret_log_findings = _release_gate_secret_log_findings(connection)
    sharing_violations = _release_gate_runtime_sharing_violations(connection)
    checks = [
        {"name": "status_ok", "ok": status["ok"] is True},
        {
            "name": "no_active_direct_bypass",
            "ok": not active_direct_bypasses,
            "findings": active_direct_bypasses,
        },
        certification_check,
        {
            "name": "mcp_hub_auth",
            "ok": not mcp_hub_auth_findings,
            "findings": mcp_hub_auth_findings,
        },
        {
            "name": "audit_secret_redaction",
            "ok": not secret_log_findings,
            "findings": secret_log_findings,
        },
        {
            "name": "runtime_share_policy",
            "ok": not sharing_violations,
            "findings": sharing_violations,
        },
        {"name": "audit_hash_chain", "ok": not tamper_findings, "findings": tamper_findings},
    ]
    if global_cutover:
        checks.extend(
            [
                _release_gate_control_plane_auth_capabilities_check(),
                _release_gate_certification_evidence_check(connection, checkpoint_dir),
                _release_gate_daemon_service_check(
                    home=home,
                    unit_dir=daemon_unit_dir,
                    systemctl_bin=daemon_systemctl_bin,
                ),
                _release_gate_legacy_catalog_import_check(connection),
            ]
        )
    ok = all(check["ok"] for check in checks)
    return {
        "schema_version": 1,
        "kind": "MCPMultiplexReleaseGate",
        "ok": ok,
        "mode": "global_cutover" if global_cutover else "local",
        "checks": checks,
        "status_summary": status["summary"],
    }


def doctor_migration_dry_run_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Parse legacy config projections without mutating source files or Multiplex state."""
    payload = _migration_dry_run_payload(Path(args.legacy_root).expanduser().resolve())
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] is True else 1


def cutover_dry_run_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Dry-run an MCP Hub to MCP Multiplex cutover without mutating files or state."""
    if args.source != "mcp-hub":
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexCutoverDryRun",
                    "ok": False,
                    "error": {"detail": f"unsupported cutover source: {args.source}"},
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    home = Path(args.home).expanduser() if args.home else None
    payload = _migration_dry_run_payload(_resolved_legacy_root(args.legacy_root, home=home))
    payload = {
        **payload,
        "kind": "MCPMultiplexCutoverDryRun",
        "source": args.source,
        "apply_supported": False,
        "located": _legacy_source_locator(home=home),
        "next_actions": [
            "review classifications",
            "install authenticated mcp_hub for supported agents",
            "run doctor release-gate before any cutover apply",
        ],
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] is True else 1


def cutover_import_catalog_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Dry-run or apply legacy MCP Hub catalog import."""
    if args.source != "mcp-hub":
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexLegacyCatalogImport",
                    "ok": False,
                    "error": {"detail": f"unsupported cutover source: {args.source}"},
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    home = Path(args.home).expanduser() if args.home else None
    try:
        catalog_path = _resolved_legacy_catalog_path(args.catalog_path, home=home)
        if args.apply:
            result = apply_legacy_mcp_hub_catalog_import(
                _state_connection(args),
                catalog_path,
                actor=args.actor,
            )
        else:
            result = plan_legacy_mcp_hub_catalog_import(catalog_path)
    except LegacyCatalogImportError as error:
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexLegacyCatalogImport",
                    "ok": False,
                    "error": {"detail": str(error)},
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexLegacyCatalogImport",
        "ok": result.ok,
        "mode": "apply" if args.apply else "dry_run",
        "result": (
            result.to_dict()
            if args.full_entries
            else result.to_summary_dict(sample_limit=args.sample_limit)
        ),
        "summary": {
            "entry_count": len(result.entries),
            "error_count": len(result.errors),
            "warning_count": len(result.warnings),
            "warnings_by_code": {
                code: len([warning for warning in result.warnings if warning["code"] == code])
                for code in sorted({warning["code"] for warning in result.warnings})
            },
        },
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if result.ok else 1


def cutover_apply_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Apply audited MCP Hub retirement only after the global gate passes."""
    if args.source != "mcp-hub":
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexCutoverApply",
                    "ok": False,
                    "error": {"detail": f"unsupported cutover source: {args.source}"},
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    if not args.confirm_retire_mcp_hub:
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexCutoverApply",
                    "ok": False,
                    "error": {
                        "detail": ("refusing cutover apply without --confirm-retire-mcp-hub")
                    },
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    connection = _state_connection(args)
    gate = _release_gate_payload(
        connection,
        checkpoint_dir=Path(args.checkpoint_dir).expanduser(),
        global_cutover=True,
        home=Path(args.home).expanduser() if args.home else None,
        daemon_unit_dir=Path(args.daemon_unit_dir).expanduser() if args.daemon_unit_dir else None,
        daemon_systemctl_bin=args.daemon_systemctl_bin,
    )
    if gate["ok"] is not True:
        payload = {
            "schema_version": 1,
            "kind": "MCPMultiplexCutoverApply",
            "ok": False,
            "source": args.source,
            "error": {"detail": "global release gate did not pass"},
            "release_gate": gate,
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 1
    event = EventStore(connection).append(
        event_id=_cutover_event_id(args.source),
        event_type="cutover.applied",
        actor=args.actor,
        result="success",
        payload={
            "source": args.source,
            "legacy_mcp_hub_deprecated": True,
            "release_gate_mode": gate["mode"],
            "release_gate_check_names": [check["name"] for check in gate["checks"]],
            "unmanaged_process_action": "none",
            "operator_note": (
                "Governor cutover recorded. This command does not stop unmanaged "
                "legacy MCP Hub processes."
            ),
        },
    )
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexCutoverApply",
        "ok": True,
        "source": args.source,
        "result": {
            "event": event.event.to_dict(),
            "legacy_mcp_hub_deprecated": True,
            "unmanaged_process_action": "none",
        },
        "release_gate": gate,
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def cutover_status_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Report whether audited MCP Hub retirement has been recorded."""
    if args.source != "mcp-hub":
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexCutoverStatus",
                    "ok": False,
                    "error": {"detail": f"unsupported cutover source: {args.source}"},
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    connection = _state_connection(args)
    latest = _latest_cutover_event(connection, args.source)
    gate = None
    if args.check_gate:
        gate = _release_gate_payload(
            connection,
            checkpoint_dir=Path(args.checkpoint_dir).expanduser(),
            global_cutover=True,
            home=Path(args.home).expanduser() if args.home else None,
            daemon_unit_dir=(
                Path(args.daemon_unit_dir).expanduser() if args.daemon_unit_dir else None
            ),
            daemon_systemctl_bin=args.daemon_systemctl_bin,
        )
    legacy_footprint = None
    if args.check_footprint:
        home = Path(args.home).expanduser() if args.home else None
        legacy_footprint = _legacy_mcp_hub_footprint_payload(
            legacy_root=_resolved_legacy_root(args.legacy_root, home=home),
            home=home,
            ps_bin=args.ps_bin,
            include_processes=not args.no_processes,
        )
    deprecated = latest is not None
    current_gate_ok = gate is None or gate["ok"] is True
    legacy_footprint_ok = legacy_footprint is None or legacy_footprint["ok"] is True
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexCutoverStatus",
        "ok": deprecated and current_gate_ok and legacy_footprint_ok,
        "source": args.source,
        "legacy_mcp_hub_deprecated": deprecated,
        "latest_event": latest.to_dict() if latest is not None else None,
        "unmanaged_process_action": (
            latest.payload.get("unmanaged_process_action") if latest is not None else None
        ),
        "release_gate": gate,
        "legacy_footprint": legacy_footprint,
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] else 1


def _latest_cutover_event(connection: sqlite3.Connection, source: str) -> Any | None:
    events = EventStore(connection).query(event_type="cutover.applied")
    matching_events = [
        record
        for record in events
        if record.payload.get("source") == source
        and record.payload.get("legacy_mcp_hub_deprecated") is True
    ]
    return matching_events[-1] if matching_events else None


def cutover_legacy_footprint_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Report remaining legacy MCP Hub footprint without mutating or stopping it."""
    if args.source != "mcp-hub":
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexLegacyFootprint",
                    "ok": False,
                    "error": {"detail": f"unsupported cutover source: {args.source}"},
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    home = Path(args.home).expanduser() if args.home else None
    payload = _legacy_mcp_hub_footprint_payload(
        legacy_root=_resolved_legacy_root(args.legacy_root, home=home),
        home=home,
        ps_bin=args.ps_bin,
        include_processes=not args.no_processes,
    )
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] is True else 1


def cutover_legacy_cleanup_plan_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Build a read-only cleanup plan for remaining legacy MCP Hub footprint."""
    if args.source != "mcp-hub":
        stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "MCPMultiplexLegacyCleanupPlan",
                    "ok": False,
                    "error": {"detail": f"unsupported cutover source: {args.source}"},
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    home = Path(args.home).expanduser() if args.home else None
    footprint = _legacy_mcp_hub_footprint_payload(
        legacy_root=_resolved_legacy_root(args.legacy_root, home=home),
        home=home,
        ps_bin=args.ps_bin,
        include_processes=not args.no_processes,
    )
    payload = _legacy_cleanup_plan_payload(footprint)
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] is True else 1


def cutover_locate_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Locate likely legacy MCP Hub roots and catalog exports."""
    home = Path(args.home).expanduser() if args.home else None
    payload = _legacy_source_locator(home=home)
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] is True else 1


def _legacy_cleanup_plan_payload(footprint: dict[str, Any]) -> dict[str, Any]:
    actions = footprint["operator_actions_required"]
    steps: list[dict[str, Any]] = []
    for action in actions:
        kind = action["kind"]
        if kind == "legacy_process_present":
            steps.append(
                {
                    "step_id": "stop_legacy_processes",
                    "action": "operator_stop_legacy_processes",
                    "approval_required": True,
                    "destructive": True,
                    "reason": "active legacy MCP Hub-like processes remain after cutover",
                    "evidence": {
                        "processes": footprint["process_scan"]["matches"],
                        "match_count": footprint["process_scan"]["match_count"],
                    },
                    "suggested_commands": [
                        "review process tree with ps -fp <pid>",
                        "stop the owning tmux/session/service explicitly if it is legacy MCP Hub",
                    ],
                }
            )
        elif kind == "legacy_service_unit_present":
            steps.append(
                {
                    "step_id": "disable_legacy_service_units",
                    "action": "operator_disable_legacy_service_units",
                    "approval_required": True,
                    "destructive": True,
                    "reason": "legacy MCP Hub service unit candidates remain",
                    "evidence": {"service_units": footprint["service_units"]},
                    "suggested_commands": [
                        f"systemctl --user disable --now {Path(unit['path']).name}"
                        for unit in footprint["service_units"]
                    ],
                }
            )
        elif kind == "legacy_repo_present":
            steps.append(
                {
                    "step_id": "archive_or_remove_legacy_repo",
                    "action": "operator_archive_or_remove_legacy_repo",
                    "approval_required": True,
                    "destructive": True,
                    "reason": "legacy MCP Hub repository/root remains after cutover",
                    "evidence": {
                        "legacy_root": footprint["legacy_root"],
                        "legacy_root_git_repository": footprint["legacy_root_git_repository"],
                        "catalog_exports": footprint["catalog_exports"],
                        "launch_scripts": footprint["launch_scripts"],
                    },
                    "suggested_commands": [
                        f"review {footprint['legacy_root']} before archive/removal",
                        f"move {footprint['legacy_root']} to an operator-approved archive path",
                    ],
                }
            )
        elif kind == "legacy_executable_present":
            steps.append(
                {
                    "step_id": "uninstall_legacy_executables",
                    "action": "operator_uninstall_legacy_executables",
                    "approval_required": True,
                    "destructive": True,
                    "reason": "legacy MCP Hub executables remain globally resolvable",
                    "evidence": {"executables": footprint["legacy_executables"]},
                    "suggested_commands": [
                        f"review executable ownership for {executable['path']}"
                        for executable in footprint["legacy_executables"]
                    ]
                    + [
                        "uninstall MCP Hub with the package manager that owns the executable",
                    ],
                }
            )
    return {
        "schema_version": 1,
        "kind": "MCPMultiplexLegacyCleanupPlan",
        "ok": not steps,
        "source": footprint["source"],
        "footprint": footprint,
        "steps": steps,
        "step_count": len(steps),
        "apply_supported": False,
        "unmanaged_process_action": "none",
        "mutation_action": "none",
    }


def _legacy_mcp_hub_footprint_payload(
    *,
    legacy_root: Path,
    home: Path | None,
    ps_bin: str,
    include_processes: bool,
) -> dict[str, Any]:
    root_exists = legacy_root.exists()
    root_is_git = (legacy_root / ".git").exists()
    catalog_exports = (
        sorted(path.name for path in legacy_root.glob("hub.json*")) if root_exists else []
    )
    launch_scripts = [
        {"name": name, "path": str(legacy_root / name), "exists": (legacy_root / name).exists()}
        for name in ("launch-hub.py", "launch-hub.sh")
    ]
    service_units = _legacy_mcp_hub_service_units(home)
    legacy_executables = _legacy_mcp_hub_executables()
    processes: list[dict[str, Any]] = []
    process_error = None
    if include_processes:
        processes, process_error = _legacy_mcp_hub_processes(ps_bin=ps_bin)
    operator_actions: list[dict[str, Any]] = []
    if root_exists:
        operator_actions.append(
            {
                "kind": "legacy_repo_present",
                "detail": (
                    "legacy MCP Hub repository still exists; archive or remove only after review"
                ),
                "path": str(legacy_root),
            }
        )
    for unit in service_units:
        operator_actions.append(
            {
                "kind": "legacy_service_unit_present",
                "detail": (
                    "legacy MCP Hub service unit candidate exists; "
                    "disable/remove separately if used"
                ),
                "path": unit["path"],
            }
        )
    for executable in legacy_executables:
        operator_actions.append(
            {
                "kind": "legacy_executable_present",
                "detail": (
                    "legacy MCP Hub executable is still globally resolvable; "
                    "uninstall it through its owning package manager"
                ),
                "path": executable["path"],
            }
        )
    if processes:
        operator_actions.append(
            {
                "kind": "legacy_process_present",
                "detail": (
                    "legacy MCP Hub-like process is running; "
                    "stop only through an explicit operator action"
                ),
                "count": len(processes),
            }
        )
    ok = process_error is None and not operator_actions
    return {
        "schema_version": 1,
        "kind": "MCPMultiplexLegacyFootprint",
        "ok": ok,
        "source": "mcp-hub",
        "legacy_root": str(legacy_root),
        "legacy_root_exists": root_exists,
        "legacy_root_git_repository": root_is_git,
        "catalog_exports": catalog_exports,
        "launch_scripts": launch_scripts,
        "service_units": service_units,
        "legacy_executables": legacy_executables,
        "process_scan": {
            "enabled": include_processes,
            "error": process_error,
            "matches": processes,
            "match_count": len(processes),
        },
        "operator_actions_required": operator_actions,
        "unmanaged_process_action": "none",
    }


def _legacy_mcp_hub_executables() -> list[dict[str, str]]:
    executables = []
    for name in ("mcp-hub",):
        path = shutil.which(name)
        if path is not None:
            executables.append({"name": name, "path": str(Path(path).resolve())})
    return executables


def _legacy_mcp_hub_service_units(home: Path | None) -> list[dict[str, Any]]:
    if home is None:
        home = Path.home()
    unit_dir = home / ".config" / "systemd" / "user"
    names = ("mcp-hub.service", "mcp_hub.service")
    units = []
    for name in names:
        path = unit_dir / name
        if path.exists():
            units.append({"name": name, "path": str(path)})
    return units


def _legacy_mcp_hub_processes(*, ps_bin: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        completed = subprocess.run(
            [ps_bin, "-eo", "pid=,ppid=,comm=,args="],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        return [], str(error)
    matches = []
    for line in completed.stdout.splitlines():
        parsed = _parse_ps_line(line)
        if parsed is None:
            continue
        args = parsed["args"]
        if _is_legacy_mcp_hub_process(args):
            matches.append(parsed)
    return matches, None


def _parse_ps_line(line: str) -> dict[str, Any] | None:
    parts = line.strip().split(None, 3)
    if len(parts) < 4:
        return None
    pid_text, ppid_text, command, args = parts
    try:
        pid = int(pid_text)
        ppid = int(ppid_text)
    except ValueError:
        return None
    return {"pid": pid, "ppid": ppid, "command": command, "args": args}


def _is_legacy_mcp_hub_process(args: str) -> bool:
    normalized = args.lower()
    if "mcp-multiplex" in normalized or "uv run mxp" in normalized or "mxp " in normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "mcp-hub",
            "mcp_hub",
            "launch-hub.py",
            "launch-hub.sh",
            "/mcp-hub/",
            "\\mcp-hub\\",
        )
    )


def _legacy_candidate_search_roots(home: Path | None) -> list[Path]:
    resolved_home = (home or Path.home()).expanduser()
    roots = [Path(pattern.replace("~", str(resolved_home))) for pattern in LEGACY_DISCOVERY_ROOTS]
    if resolved_home not in roots:
        roots.append(resolved_home)
    return roots


def _legacy_source_candidates(*, home: Path | None = None) -> list[dict[str, Any]]:
    resolved_home = (home or Path.home()).expanduser().resolve()
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in _legacy_candidate_search_roots(home):
        if root.name == "mcp-hub" and root.exists():
            candidate_dirs = [root]
        elif root.is_dir():
            try:
                candidate_dirs = [item for item in root.iterdir() if item.is_dir()]
            except OSError:
                candidate_dirs = []
        else:
            candidate_dirs = []
        for candidate in candidate_dirs:
            candidate = candidate.expanduser().resolve()
            if str(candidate) in seen:
                continue
            if candidate.is_dir():
                try:
                    names = {item.name for item in candidate.iterdir()}
                except OSError:
                    continue
            else:
                names = set()
            if (
                candidate.name == "mcp-hub"
                or "mcp-hub" in candidate.name
                or "launch-hub.py" in names
                or "launch-hub.sh" in names
                or any(name.startswith("hub.json") for name in names)
            ):
                seen.add(str(candidate))
                exports = sorted(
                    str(item)
                    for item in candidate.iterdir()
                    if item.is_file() and item.name.startswith("hub.json")
                )
                score = int((candidate / ".git").exists()) * 3
                score += int("launch-hub.py" in names or "launch-hub.sh" in names) * 2
                score += min(len(exports), 3)
                candidates.append(
                    {
                        "path": str(candidate),
                        "git_repository": (candidate / ".git").exists(),
                        "catalog_exports": exports,
                        "launch_scripts": sorted(
                            name for name in names if name in {"launch-hub.py", "launch-hub.sh"}
                        ),
                        "preferred": str(candidate).startswith(str(resolved_home)),
                        "score": score,
                    }
                )
    return sorted(
        candidates,
        key=lambda item: (
            -int(bool(item["preferred"])),
            -int(item["score"]),
            str(item["path"]),
        ),
    )


def _legacy_source_locator(*, home: Path | None = None) -> dict[str, Any]:
    candidates = _legacy_source_candidates(home=home)
    selected = candidates[0] if candidates else None
    return {
        "schema_version": 1,
        "kind": "MCPMultiplexLegacyLocate",
        "ok": selected is not None,
        "candidates": candidates,
        "selected_legacy_root": None if selected is None else selected["path"],
        "selected_catalog_path": (
            None
            if selected is None or not selected["catalog_exports"]
            else selected["catalog_exports"][0]
        ),
    }


def _resolved_legacy_root(path: str | None, *, home: Path | None = None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    located = _legacy_source_locator(home=home)
    if located["selected_legacy_root"] is not None:
        return Path(str(located["selected_legacy_root"])).expanduser().resolve()
    return ((home or Path.home()).expanduser() / "mcp-hub").resolve()


def _resolved_legacy_catalog_path(path: str | None, *, home: Path | None = None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    located = _legacy_source_locator(home=home)
    if located["selected_catalog_path"] is not None:
        return Path(str(located["selected_catalog_path"])).expanduser().resolve()
    raise LegacyCatalogImportError("no legacy MCP Hub catalog export was discovered")


def _migration_dry_run_payload(legacy_root: Path) -> dict[str, Any]:
    discovered = discover_config_paths(home=legacy_root)
    signatures_before = {
        str(item.path): _file_digest(Path(item.path))
        for item in discovered.config_paths
        if Path(item.path).is_file()
    }
    observed = []
    errors = []
    for item in discovered.config_paths:
        watched = WatchedConfigPath.from_discovered(item)
        try:
            observed.extend(parse_watched_config(watched))
        except Exception as error:  # noqa: BLE001 - surfaced as dry-run finding, not swallowed.
            errors.append({"path": str(item.path), "detail": str(error)})
    signatures_after = {
        path: _file_digest(Path(path)) for path in signatures_before if Path(path).is_file()
    }
    mutated_paths = [
        path
        for path, before_digest in sorted(signatures_before.items())
        if signatures_after.get(path) != before_digest
    ]
    classifications = classify_observed_entries(observed)
    health = health_payload_for_classifications(observed, classifications)
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexMigrationDryRun",
        "ok": not errors and not mutated_paths,
        "legacy_root": str(legacy_root),
        "discovered": discovered.to_dict(),
        "observed_count": len(observed),
        "classifications": [item.to_dict() for item in classifications],
        "health": health,
        "mutated_paths": mutated_paths,
        "errors": errors,
    }
    return payload


def _agent_onboarding_payload(
    connection: sqlite3.Connection,
    *,
    home: Path | None = None,
    agent_kinds: list[str] | None = None,
) -> dict[str, Any]:
    if home is None:
        return {
            "discovered_config_paths": [],
            "discovery_notices": [],
            "unregistered_agents": [],
            "unregistered_count": 0,
            "next_action": None,
        }
    discovery = discover_config_paths(home=home, agent_kinds=agent_kinds)
    registered_kinds = {agent.agent_kind for agent in AgentRegistry(connection).list()}
    unregistered = [
        item.to_dict() for item in discovery.config_paths if item.agent_kind not in registered_kinds
    ]
    next_action = None
    if unregistered:
        resolved_home = (home or Path.home()).expanduser()
        next_action = f"mxp agents sync --apply --home {resolved_home}"
    return {
        "discovered_config_paths": [item.to_dict() for item in discovery.config_paths],
        "discovery_notices": [notice.to_dict() for notice in discovery.notices],
        "unregistered_agents": unregistered,
        "unregistered_count": len(unregistered),
        "next_action": next_action,
    }


def _augment_status_with_onboarding(
    payload: dict[str, Any],
    *,
    connection: sqlite3.Connection,
    home: Path | None = None,
) -> None:
    onboarding = _agent_onboarding_payload(connection, home=home)
    payload["onboarding"] = onboarding
    if onboarding["unregistered_count"] == 0:
        return
    payload["ok"] = False
    notices = list(payload.get("notices", []))
    notices.append(
        {
            "code": "agent_configs_discovered_but_unregistered",
            "detail": (
                f"discovered {onboarding['unregistered_count']} agent config paths that are not "
                "registered in Multiplex state"
            ),
        }
    )
    payload["notices"] = notices
    summary = payload.get("summary")
    if isinstance(summary, dict):
        summary["notices"] = int(summary.get("notices", 0)) + 1


EXPECTED_CERTIFICATION_TRANSCRIPTS = {
    "codex": "TASK-038-codex-certification.md",
    "claude_code": "TASK-039-claude-code-certification.md",
    "gemini": "TASK-040-gemini-certification.md",
    "cline": "TASK-041-cline-certification.md",
    "opencode": "TASK-042-opencode-certification.md",
}
DEFAULT_CERTIFICATION_DIR = "docs/certifications"

RAW_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
)
SECRET_KEY_PATTERN = re.compile(r"(secret|token|password|passwd|api[_-]?key|credential)", re.I)
SAFE_SECRET_VALUES = {"", "<redacted>", "[redacted]", "***", "secret_values_removed"}
SAFE_SECRET_REF_KEYS = {"token_ref", "secret_ref", "source_ref", "credential_ref"}


def _release_gate_certification_check(checkpoint_dir: Path) -> dict[str, Any]:
    findings = []
    for client, filename in EXPECTED_CERTIFICATION_TRANSCRIPTS.items():
        path = checkpoint_dir / filename
        if not path.is_file():
            findings.append({"client": client, "path": str(path), "detail": "missing transcript"})
            continue
        text = path.read_text(encoding="utf-8")
        if "Result: PASS" not in text:
            findings.append(
                {"client": client, "path": str(path), "detail": "certification not PASS"}
            )
    return {
        "name": "real_client_certifications",
        "ok": not findings,
        "checkpoint_dir": str(checkpoint_dir),
        "findings": findings,
    }


def _release_gate_certification_evidence_check(
    connection: sqlite3.Connection,
    checkpoint_dir: Path,
) -> dict[str, Any]:
    """Require hash-bound audit evidence for global cutover certification transcripts."""
    findings = []
    imported = _certification_evidence_by_client(connection)
    for client, filename in EXPECTED_CERTIFICATION_TRANSCRIPTS.items():
        path = checkpoint_dir / filename
        if not path.is_file():
            findings.append({"client": client, "path": str(path), "detail": "missing transcript"})
            continue
        transcript_hash = _file_digest(path)
        evidence = imported.get(client)
        if evidence is None:
            findings.append(
                {
                    "client": client,
                    "path": str(path),
                    "transcript_hash": transcript_hash,
                    "detail": "missing hash-bound certification evidence event",
                }
            )
            continue
        if evidence.get("transcript_hash") != transcript_hash:
            finding: dict[str, Any] = {
                "client": client,
                "path": str(path),
                "transcript_hash": transcript_hash,
                "evidence_hash": evidence.get("transcript_hash"),
                "detail": "checkpoint hash does not match imported evidence",
            }
            findings.append(finding)
            continue
        if evidence.get("result") != "PASS":
            findings.append(
                {
                    "client": client,
                    "path": str(path),
                    "transcript_hash": transcript_hash,
                    "detail": "imported certification evidence is not PASS",
                }
            )
    return {
        "name": "certification_evidence_hashes",
        "ok": not findings,
        "checkpoint_dir": str(checkpoint_dir),
        "findings": findings,
    }


def _certification_evidence_by_client(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Return latest imported certification evidence payload for each client."""
    evidence: dict[str, dict[str, Any]] = {}
    for record in EventStore(connection).query(event_type="certification.evidence_imported"):
        client = record.payload.get("client")
        if isinstance(client, str):
            evidence[client] = record.payload
    return evidence


def _release_gate_control_plane_auth_capabilities_check() -> dict[str, Any]:
    """Require first-wave agents to have safe automatic mcp_hub auth installs."""
    capabilities = control_plane_auth_capabilities()
    findings: list[dict[str, Any]] = []
    for capability in capabilities:
        agent_kind = capability.agent_kind
        if not capability.automatic_install_supported:
            findings.append(
                {
                    "agent_kind": agent_kind,
                    "status": capability.status,
                    "detail": "automatic authenticated control-plane install is not supported",
                }
            )
        if capability.status != "implemented":
            findings.append(
                {
                    "agent_kind": agent_kind,
                    "status": capability.status,
                    "detail": "authenticated control-plane installer is not implemented",
                }
            )
        if capability.raw_token_storage_required:
            findings.append(
                {
                    "agent_kind": agent_kind,
                    "status": capability.status,
                    "detail": "authenticated control-plane install requires raw token storage",
                }
            )
    return {
        "name": "control_plane_auth_capabilities",
        "ok": not findings,
        "capabilities": [capability.to_dict() for capability in capabilities],
        "findings": findings,
    }


def _release_gate_mcp_hub_auth_findings(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
          observed_entry_id,
          agent_id,
          agent_kind,
          config_path,
          url,
          headers_present_json
        FROM observed_entries
        WHERE mount_name = 'mcp_hub'
          AND enabled = 1
        ORDER BY agent_id, config_path, observed_entry_id
        """
    ).fetchall()
    findings: list[dict[str, Any]] = []
    expected_url = f"{HUB_ORIGIN}/servers/mcp_hub/mcp"
    for row in rows:
        try:
            headers_present = json.loads(str(row["headers_present_json"]))
        except json.JSONDecodeError:
            headers_present = []
        normalized_headers = {
            str(header).lower() for header in headers_present if isinstance(header, str)
        }
        if row["url"] != expected_url:
            findings.append(
                {
                    "observed_entry_id": str(row["observed_entry_id"]),
                    "agent_id": str(row["agent_id"]),
                    "agent_kind": str(row["agent_kind"]),
                    "config_path": str(row["config_path"]),
                    "detail": "mcp_hub is not routed to the Multiplex control-plane URL",
                }
            )
            continue
        if "authorization" not in normalized_headers:
            findings.append(
                {
                    "observed_entry_id": str(row["observed_entry_id"]),
                    "agent_id": str(row["agent_id"]),
                    "agent_kind": str(row["agent_kind"]),
                    "config_path": str(row["config_path"]),
                    "detail": "mcp_hub control-plane entry lacks Authorization header metadata",
                }
            )
    return findings


def _release_gate_secret_log_findings(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    rows = connection.execute(
        """
        SELECT event_id, event_type, redaction, payload_json
        FROM events
        ORDER BY timestamp, event_id
        """
    ).fetchall()
    for row in rows:
        event_id = str(row["event_id"])
        if row["redaction"] != "secret_values_removed":
            findings.append(
                {
                    "event_id": event_id,
                    "event_type": str(row["event_type"]),
                    "detail": "event does not declare secret redaction",
                }
            )
        payload_text = str(row["payload_json"])
        for pattern in RAW_SECRET_PATTERNS:
            if pattern.search(payload_text):
                findings.append(
                    {
                        "event_id": event_id,
                        "event_type": str(row["event_type"]),
                        "detail": "payload contains raw secret-looking value",
                    }
                )
                break
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        findings.extend(_secret_key_findings(event_id, str(row["event_type"]), payload))
    return findings


def _secret_key_findings(
    event_id: str,
    event_type: str,
    value: Any,
    *,
    path: str = "$",
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if (
                SECRET_KEY_PATTERN.search(str(key))
                and isinstance(item, str)
                and not _safe_secret_reference_value(str(key), item)
            ):
                findings.append(
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "path": child_path,
                        "detail": "secret-like key contains non-reference value",
                    }
                )
            findings.extend(_secret_key_findings(event_id, event_type, item, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(
                _secret_key_findings(event_id, event_type, item, path=f"{path}[{index}]")
            )
    return findings


def _safe_secret_reference_value(key: str, value: str) -> bool:
    return (
        key in SAFE_SECRET_REF_KEYS
        or value.startswith("secretref:")
        or value in SAFE_SECRET_VALUES
        or value.startswith("sha256:")
    )


def _release_gate_runtime_sharing_violations(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
          runtime_backends.backend_id,
          runtime_backends.catalog_id,
          runtime_backends.runtime_pool_key,
          runtime_backends.frontend_session_count,
          catalog_entries.name,
          catalog_entries.runtime_json
        FROM runtime_backends
        LEFT JOIN catalog_entries ON catalog_entries.catalog_id = runtime_backends.catalog_id
        WHERE runtime_backends.state IN ('starting', 'hot', 'idle')
        ORDER BY runtime_backends.backend_id
        """
    ).fetchall()
    violations: list[dict[str, Any]] = []
    for row in rows:
        backend_id = str(row["backend_id"])
        try:
            runtime = (
                json.loads(str(row["runtime_json"])) if row["runtime_json"] is not None else {}
            )
        except json.JSONDecodeError:
            runtime = {}
        shareability = runtime.get("shareability")
        pool_key = str(row["runtime_pool_key"])
        frontend_count = int(row["frontend_session_count"])
        if shareability == "no_proxy":
            violations.append(
                {
                    "backend_id": backend_id,
                    "catalog_id": str(row["catalog_id"]),
                    "detail": "no_proxy catalog entry has a runtime backend",
                }
            )
        if shareability != "global" and pool_key.startswith("global:"):
            violations.append(
                {
                    "backend_id": backend_id,
                    "catalog_id": str(row["catalog_id"]),
                    "detail": "non-global catalog entry is using a global runtime pool",
                }
            )
        if shareability != "global" and frontend_count > 1:
            violations.append(
                {
                    "backend_id": backend_id,
                    "catalog_id": str(row["catalog_id"]),
                    "detail": "non-shareable backend has multiple frontend sessions",
                }
            )
    return violations


def _release_gate_daemon_service_check(
    *,
    home: Path | None,
    unit_dir: Path | None,
    systemctl_bin: str,
) -> dict[str, Any]:
    status = user_service_status(
        home=home,
        unit_dir=unit_dir,
        systemctl_bin=systemctl_bin,
        include_systemctl=True,
    )
    findings: list[dict[str, Any]] = []
    service = status.systemctl
    if not status.unit_exists:
        findings.append({"detail": "mcp-multiplex.service unit file is not installed"})
    if not status.systemctl_available:
        findings.append({"detail": "systemctl is not available", "error": status.error})
    elif not status.systemctl_ok:
        findings.append({"detail": "systemctl --user show failed", "error": status.error})
    else:
        if service.get("LoadState") != "loaded":
            findings.append(
                {
                    "detail": "mcp-multiplex.service is not loaded",
                    "LoadState": service.get("LoadState"),
                }
            )
        if service.get("ActiveState") != "active":
            findings.append(
                {
                    "detail": "mcp-multiplex.service is not active",
                    "ActiveState": service.get("ActiveState"),
                }
            )
        if service.get("UnitFileState") not in {"enabled", "linked", "linked-runtime"}:
            findings.append(
                {
                    "detail": "mcp-multiplex.service is not enabled",
                    "UnitFileState": service.get("UnitFileState"),
                }
            )
    return {
        "name": "daemon_user_service",
        "ok": not findings,
        "status": status.to_dict(),
        "findings": findings,
    }


def _release_gate_legacy_catalog_import_check(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    import_events = EventStore(connection).query(event_type="catalog.legacy_import")
    findings: list[dict[str, Any]] = []
    if not import_events:
        findings.append({"detail": "no legacy MCP Hub catalog import audit event found"})
    rows = connection.execute(
        """
        SELECT
          catalog_entries.catalog_id,
          catalog_entries.name,
          catalog_entries.review_state,
          catalog_entries.lifecycle_state
        FROM catalog_entries
        INNER JOIN catalog_provenance
          ON catalog_provenance.catalog_id = catalog_entries.catalog_id
        WHERE catalog_provenance.source = 'legacy_mcp_hub'
        ORDER BY catalog_entries.catalog_id
        """
    ).fetchall()
    if not rows:
        findings.append({"detail": "no catalog entries with legacy_mcp_hub provenance found"})
    for row in rows:
        review_state = str(row["review_state"])
        lifecycle_state = str(row["lifecycle_state"])
        if review_state != "approved":
            findings.append(
                {
                    "catalog_id": str(row["catalog_id"]),
                    "name": str(row["name"]),
                    "detail": "legacy imported catalog entry is not approved",
                    "review_state": review_state,
                }
            )
        if lifecycle_state != "enabled":
            findings.append(
                {
                    "catalog_id": str(row["catalog_id"]),
                    "name": str(row["name"]),
                    "detail": "legacy imported catalog entry is not enabled",
                    "lifecycle_state": lifecycle_state,
                }
            )
    return {
        "name": "legacy_catalog_import",
        "ok": not findings,
        "import_event_count": len(import_events),
        "legacy_catalog_entry_count": len(rows),
        "findings": findings,
    }


def _file_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _hash_json(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _catalog_review_event_id(catalog_id: str, before_state: str, after_state: str) -> str:
    digest = hashlib.sha256(
        f"{catalog_id}\0{before_state}\0{after_state}\0{time.time_ns()}".encode()
    ).hexdigest()
    return f"evt_{digest[:24]}"


def _cutover_event_id(source: str) -> str:
    digest = hashlib.sha256(f"{source}\0{time.time_ns()}".encode()).hexdigest()
    return f"evt_{digest[:24]}"


def _certification_evidence_event_id(client: str, transcript_hash: str) -> str:
    digest = hashlib.sha256(f"{client}\0{transcript_hash}\0{time.time_ns()}".encode()).hexdigest()
    return f"evt_{digest[:24]}"


def config_inspect_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Inspect resolved config/state/cache paths and policy config."""
    home = Path(args.home).expanduser() if args.home else None
    try:
        payload = inspect_config(home=home)
    except ConfigLoadError as error:
        error_payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "MCPMultiplexConfigInspect",
            "paths": {},
            "policy": None,
            "policy_source": str(error.path),
            "policy_exists": error.path.exists(),
            "warnings": [],
            "errors": [{"path": str(error.path), "detail": error.message}],
        }
        stdout.write(json.dumps(error_payload, sort_keys=True) + "\n")
        return 2

    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def config_discover_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Discover existing first-wave agent config paths."""
    home = Path(args.home).expanduser() if args.home else None
    agent_kinds = args.agents.split(",") if args.agents else None
    try:
        payload = discover_config_paths(home=home, agent_kinds=agent_kinds).to_dict()
    except ValueError as error:
        payload = {
            "schema_version": 1,
            "kind": "MCPMultiplexConfigDiscovery",
            "config_paths": [],
            "notices": [],
            "errors": [{"detail": str(error)}],
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def approval_list_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """List approval tasks from local Multiplex state."""
    connection = _approval_connection(args)
    approvals = ApprovalStore(connection).list(state=args.state, plan_id=args.plan_id)
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexApprovalList",
        "approvals": [approval.to_dict() for approval in approvals],
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def approval_approve_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Approve a pending approval task."""
    return _approval_decision_command(args, stdout, decision="approve")


def approval_reject_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Reject a pending approval task."""
    return _approval_decision_command(args, stdout, decision="reject")


def tui_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Render the operator TUI or perform an explicit TUI approval action."""
    from mcp_multiplex.tui import approve_from_tui, render_tui, run_tui_repl

    connection = _state_connection(args)
    tui_legacy_root = Path(args.legacy_root).expanduser() if args.legacy_root else None
    tui_home = Path(args.home).expanduser() if args.home else None
    if args.repl:
        return run_tui_repl(
            connection,
            stdin=sys.stdin,
            stdout=stdout,
            actor=args.actor,
            comment=args.comment,
            legacy_root=tui_legacy_root,
            home=tui_home,
            include_processes=not args.no_processes,
        )
    if args.approve:
        try:
            payload = approve_from_tui(
                connection,
                args.approve,
                actor=args.actor,
                comment=args.comment,
            )
        except (ApprovalError, KeyError) as error:
            payload = {
                "schema_version": 1,
                "kind": "MCPMultiplexTUIApprovalDecision",
                "ok": False,
                "error": {"detail": str(error)},
            }
            stdout.write(json.dumps(payload, sort_keys=True) + "\n")
            return 2
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 0

    screen = render_tui(
        connection,
        legacy_root=tui_legacy_root,
        home=tui_home,
        include_processes=not args.no_processes,
    )
    stdout.write(screen.text)
    return 0 if screen.snapshot["dashboard"]["ok"] is True else 1


def certify_codex_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Run Codex CLI real-client certification."""
    from mcp_multiplex.certification import CertificationError, run_codex_certification

    try:
        result = run_codex_certification(
            work_dir=Path(args.work_dir).expanduser() if args.work_dir else None,
            codex_bin=args.codex_bin,
        )
    except CertificationError as error:
        payload = {
            "schema_version": 1,
            "kind": "MCPMultiplexCodexCertification",
            "ok": False,
            "error": {"detail": str(error)},
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2
    if args.transcript:
        transcript_path = Path(args.transcript).expanduser()
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(result.transcript(), encoding="utf-8")
    stdout.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
    return 0 if result.ok else 1


def certify_claude_code_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Run Claude Code real-client certification."""
    from mcp_multiplex.certification import CertificationError, run_claude_code_certification

    try:
        result = run_claude_code_certification(
            work_dir=Path(args.work_dir).expanduser() if args.work_dir else None,
            claude_bin=args.claude_bin,
        )
    except CertificationError as error:
        payload = {
            "schema_version": 1,
            "kind": "MCPMultiplexClaudeCodeCertification",
            "ok": False,
            "error": {"detail": str(error)},
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2
    if args.transcript:
        transcript_path = Path(args.transcript).expanduser()
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(result.transcript(), encoding="utf-8")
    stdout.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
    return 0 if result.ok else 1


def certify_gemini_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Run Gemini CLI real-client certification."""
    from mcp_multiplex.certification import CertificationError, run_gemini_certification

    try:
        result = run_gemini_certification(
            work_dir=Path(args.work_dir).expanduser() if args.work_dir else None,
            gemini_bin=args.gemini_bin,
        )
    except CertificationError as error:
        payload = {
            "schema_version": 1,
            "kind": "MCPMultiplexGeminiCertification",
            "ok": False,
            "error": {"detail": str(error)},
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2
    if args.transcript:
        transcript_path = Path(args.transcript).expanduser()
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(result.transcript(), encoding="utf-8")
    stdout.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
    return 0 if result.ok else 1


def certify_cline_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Run Cline real-client certification."""
    from mcp_multiplex.certification import CertificationError, run_cline_certification

    try:
        result = run_cline_certification(
            work_dir=Path(args.work_dir).expanduser() if args.work_dir else None,
            cline_bin=args.cline_bin,
        )
    except CertificationError as error:
        payload = {
            "schema_version": 1,
            "kind": "MCPMultiplexClineCertification",
            "ok": False,
            "error": {"detail": str(error)},
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2
    if args.transcript:
        transcript_path = Path(args.transcript).expanduser()
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(result.transcript(), encoding="utf-8")
    stdout.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
    return 0 if result.ok else 1


def certify_opencode_command(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Run OpenCode real-client certification."""
    from mcp_multiplex.certification import CertificationError, run_opencode_certification

    try:
        result = run_opencode_certification(
            work_dir=Path(args.work_dir).expanduser() if args.work_dir else None,
            opencode_bin=args.opencode_bin,
        )
    except CertificationError as error:
        payload = {
            "schema_version": 1,
            "kind": "MCPMultiplexOpenCodeCertification",
            "ok": False,
            "error": {"detail": str(error)},
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2
    if args.transcript:
        transcript_path = Path(args.transcript).expanduser()
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(result.transcript(), encoding="utf-8")
    stdout.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
    return 0 if result.ok else 1


def certify_import_evidence_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Import real-client certification transcript hashes into the audit chain."""
    checkpoint_dir = Path(args.checkpoint_dir).expanduser()
    connection = _state_connection(args)
    imported = []
    findings = []
    store = EventStore(connection)
    for client, filename in EXPECTED_CERTIFICATION_TRANSCRIPTS.items():
        path = checkpoint_dir / filename
        if not path.is_file():
            findings.append({"client": client, "path": str(path), "detail": "missing transcript"})
            continue
        text = path.read_text(encoding="utf-8")
        transcript_hash = _file_digest(path)
        result = "PASS" if "Result: PASS" in text else "FAIL"
        if result != "PASS":
            findings.append(
                {
                    "client": client,
                    "path": str(path),
                    "transcript_hash": transcript_hash,
                    "detail": "certification not PASS",
                }
            )
            continue
        existing = _certification_evidence_by_client(connection).get(client)
        if existing is not None and existing.get("transcript_hash") == transcript_hash:
            imported.append(
                {
                    "client": client,
                    "path": str(path),
                    "transcript_hash": transcript_hash,
                    "event_id": None,
                    "already_imported": True,
                }
            )
            continue
        event = store.append(
            event_id=_certification_evidence_event_id(client, transcript_hash),
            event_type="certification.evidence_imported",
            actor=args.actor,
            result="success",
            payload={
                "client": client,
                "checkpoint_filename": filename,
                "path": str(path),
                "transcript_hash": transcript_hash,
                "result": "PASS",
            },
            target_path=str(path),
            after_hash=transcript_hash,
        )
        imported.append(
            {
                "client": client,
                "path": str(path),
                "transcript_hash": transcript_hash,
                "event_id": event.event.event_id,
                "event_hash": event.event.event_hash,
                "already_imported": False,
            }
        )
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexCertificationEvidenceImport",
        "ok": not findings,
        "checkpoint_dir": str(checkpoint_dir),
        "imported": imported,
        "findings": findings,
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["ok"] else 1


def _approval_decision_command(
    args: argparse.Namespace,
    stdout: TextIO,
    *,
    decision: str,
) -> int:
    connection = _approval_connection(args)
    store = ApprovalStore(connection)
    try:
        if decision == "approve":
            approval = store.approve(
                args.approval_id,
                actor=args.actor,
                channel="cli",
                comment=args.comment,
            )
        else:
            approval = store.reject(
                args.approval_id,
                actor=args.actor,
                channel="cli",
                comment=args.comment,
            )
    except (ApprovalError, KeyError) as error:
        payload = {
            "schema_version": 1,
            "kind": "MCPMultiplexApprovalDecision",
            "ok": False,
            "error": {"detail": str(error)},
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2
    payload = {
        "schema_version": 1,
        "kind": "MCPMultiplexApprovalDecision",
        "ok": True,
        "approval": approval.to_dict(),
        "plan_status": store.plan_status(approval.plan_id),
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def status_payload(connection: sqlite3.Connection) -> dict[str, Any]:
    """Build local status without resolving secret values."""
    observed_entries = ObservedEntryStore(connection).list()
    base = health_payload_for_classifications(
        observed_entries,
        classify_observed_entries(observed_entries),
    )
    summary = dict(base["summary"])
    summary["hot_backends"] = len(
        [backend for backend in RuntimeBackendStore(connection).list() if backend.state == "hot"]
    )
    summary["pending_approvals"] = len(ApprovalStore(connection).list(state="pending"))
    active_catalog_ids = {
        str(row["catalog_id"])
        for row in connection.execute(
            """
            SELECT DISTINCT catalog_id
            FROM runtime_backends
            WHERE state IN ('starting', 'hot', 'idle')
            """
        ).fetchall()
    }
    credentials = CredentialRefStore(connection).list()
    credential_status = readiness_summary(credentials, active_catalog_ids=active_catalog_ids)
    blockers = list(base["blockers"])
    warnings = list(base["warnings"])
    for item in credential_status.blockers:
        blockers.append(
            {
                "area": "credentials",
                "code": item["code"],
                "detail": f"{item['name']} is {item['readiness_state']} for active catalog entry",
                "server": item["catalog_id"],
            }
        )
    for item in credential_status.warnings:
        warnings.append(
            {
                "area": "credentials",
                "code": item["code"],
                "detail": f"{item['name']} is {item['readiness_state']} for dormant catalog entry",
                "server": item["catalog_id"],
            }
        )
    summary["blockers"] = len(blockers)
    summary["warnings"] = len(warnings)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "MCPMultiplexHealth",
        "ok": not blockers,
        "summary": summary,
        "blockers": blockers,
        "warnings": warnings,
        "notices": base["notices"],
    }
    return payload


def compact_status(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    state = "ok" if payload["ok"] else "blocked"
    return (
        f"{state}: {summary['blockers']} blockers, {summary['warnings']} warnings, "
        f"{summary['active_servers']} active servers, {summary['hot_backends']} hot backends, "
        f"{summary['pending_approvals']} pending approvals"
    )


def _approval_connection(args: argparse.Namespace) -> sqlite3.Connection:
    return _state_connection(args)


def _state_connection(args: argparse.Namespace) -> sqlite3.Connection:
    db_path = _db_path_from_args(args)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return connect(db_path)


def _db_path_from_args(args: argparse.Namespace) -> Path:
    if args.db_path:
        return Path(args.db_path).expanduser()
    home = Path(args.home).expanduser() if args.home else None
    layout = resolve_environment_layout(home=home)
    return layout.state_dir / "multiplex.db"


def _registered_watch_targets(connection: sqlite3.Connection) -> list[WatchedConfigPath]:
    targets: list[WatchedConfigPath] = []
    for agent in AgentRegistry(connection).list():
        for config_path in agent.config_paths:
            targets.append(
                WatchedConfigPath(
                    agent_id=agent.agent_id,
                    agent_kind=agent.agent_kind,
                    path=Path(config_path.path),
                    format=config_path.format,
                    precedence=config_path.precedence,
                    is_project_shared=config_path.is_project_shared,
                )
            )
    return targets


def _insert_remediation_plan(connection: sqlite3.Connection, plan: RemediationPlan) -> bool:
    with connection:
        cursor = connection.execute(
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id) DO NOTHING
            """,
            (
                plan.plan_id,
                plan.schema_version,
                plan.plan_type,
                plan.status,
                plan.agent_id,
                plan.target_path,
                plan.observed_entry_id,
                plan.catalog_id,
                json.dumps(plan.policy, sort_keys=True),
                plan.diff.format,
                plan.diff.text,
                plan.expected_preimage_hash,
                plan.rollback_strategy,
                json.dumps(plan.risk, sort_keys=True),
                plan.created_at,
            ),
        )
    return cursor.rowcount == 1


def _plan_rows(
    connection: sqlite3.Connection, *, status: str | None = None
) -> list[dict[str, object]]:
    clauses: list[str] = []
    params: list[str] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        f"""
        SELECT plan_id
        FROM remediation_plans
        {where}
        ORDER BY created_at, plan_id
        """,
        params,
    ).fetchall()
    return [_plan_row(connection, str(row["plan_id"])) for row in rows]


def _plan_row(connection: sqlite3.Connection, plan_id: str) -> dict[str, object]:
    row = connection.execute(
        """
        SELECT *
        FROM remediation_plans
        WHERE plan_id = ?
        """,
        (plan_id,),
    ).fetchone()
    if row is None:
        raise KeyError(plan_id)
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


def _runtime_sharing_explanation(runtime_pool_key: str) -> str:
    if runtime_pool_key.startswith("global:"):
        return "shared globally because catalog shareability is global"
    if runtime_pool_key.startswith("workspace:"):
        return "isolated by workspace root"
    if runtime_pool_key.startswith("agent:"):
        return "isolated by invoking agent"
    if runtime_pool_key.startswith("account:"):
        return "isolated by account scope"
    if runtime_pool_key.startswith("isolated:"):
        return "not shared; isolated per frontend session or remote runtime"
    return "runtime pool key uses an unknown isolation strategy"


def build_parser(prog: str = "mxp") -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--version", action="version", version=f"mxp {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    health = subcommands.add_parser("health", help="query daemon health")
    health.add_argument("--host", default=DEFAULT_HOST)
    health.add_argument("--port", default=DEFAULT_PORT, type=int)
    health.add_argument("--timeout", default=2.0, type=float)
    health.set_defaults(handler=health_command)

    daemon = subcommands.add_parser("daemon", help="manage the local daemon service")
    daemon_subcommands = daemon.add_subparsers(dest="daemon_command")
    daemon_install_service = daemon_subcommands.add_parser(
        "install-user-service",
        help="install the daemon as a systemd user service",
    )
    daemon_install_service.add_argument(
        "--apply",
        action="store_true",
        help="write the unit after backup and verification; default is dry-run",
    )
    daemon_install_service.add_argument("--home", default=None, help="override home path root")
    daemon_install_service.add_argument(
        "--db-path",
        default=None,
        help="path to Multiplex SQLite state used by both CLI and daemon",
    )
    daemon_install_service.add_argument(
        "--unit-dir",
        default=None,
        help="override systemd user unit directory for tests or custom installs",
    )
    daemon_install_service.add_argument(
        "--daemon-bin",
        default=None,
        help="daemon executable path; defaults to mcp-multiplex-daemon on PATH",
    )
    daemon_install_service.add_argument("--host", default=DEFAULT_HOST)
    daemon_install_service.add_argument("--port", default=DEFAULT_PORT, type=int)
    daemon_install_service.add_argument("--actor", default="local_operator")
    daemon_install_service.set_defaults(handler=daemon_install_user_service_command)
    daemon_status = daemon_subcommands.add_parser(
        "status",
        help="show daemon user-service status without mutating service state",
    )
    daemon_status.add_argument("--home", default=None, help="override home path root")
    daemon_status.add_argument(
        "--unit-dir",
        default=None,
        help="override systemd user unit directory for tests or custom installs",
    )
    daemon_status.add_argument(
        "--systemctl-bin",
        default="systemctl",
        help="systemctl executable to query; defaults to PATH lookup",
    )
    daemon_status.add_argument(
        "--no-systemctl",
        action="store_true",
        help="inspect only the expected unit file path",
    )
    daemon_status.set_defaults(handler=daemon_status_command)

    status = subcommands.add_parser("status", help="show local Multiplex status")
    status.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    status.add_argument("--home", default=None, help="override home for default state path")
    output_group = status.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="emit stable JSON status")
    output_group.add_argument("--compact", action="store_true", help="emit compact text status")
    status.set_defaults(handler=status_command)

    audit = subcommands.add_parser("audit", help="run observe-only audits")
    audit_subcommands = audit.add_subparsers(dest="audit_command")
    audit_run = audit_subcommands.add_parser("run", help="run config audit over registered paths")
    audit_run.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    audit_run.add_argument("--home", default=None, help="override home for default state path")
    audit_run.add_argument("--run-id", default=None)
    audit_run.add_argument("--actor", default="cli")
    audit_run.set_defaults(handler=audit_run_command)
    audit_plan = audit_subcommands.add_parser(
        "plan",
        help="generate and store dry-run remediation plans over registered paths",
    )
    audit_plan.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    audit_plan.add_argument("--home", default=None, help="override home for default state path")
    audit_plan.add_argument("--run-id", default=None)
    audit_plan.add_argument("--actor", default="cli")
    audit_plan.add_argument(
        "--skip-missing-control-plane",
        action="store_true",
        help="do not generate plans for missing mcp_hub entries",
    )
    audit_plan.set_defaults(handler=audit_plan_command)

    plan = subcommands.add_parser("plan", help="list and inspect remediation plans")
    plan_subcommands = plan.add_subparsers(dest="plan_command")
    plan_list = plan_subcommands.add_parser("list", help="list remediation plans")
    plan_list.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    plan_list.add_argument("--home", default=None, help="override home for default state path")
    plan_list.add_argument("--status", default=None, help="filter by plan status")
    plan_list.set_defaults(handler=plan_list_command)
    plan_show = plan_subcommands.add_parser("show", help="show one remediation plan")
    plan_show.add_argument("plan_id")
    plan_show.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    plan_show.add_argument("--home", default=None, help="override home for default state path")
    plan_show.set_defaults(handler=plan_show_command)

    apply_parser = subcommands.add_parser("apply", help="apply an approved remediation plan")
    apply_parser.add_argument("plan_id")
    apply_parser.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    apply_parser.add_argument("--home", default=None, help="override home for default state path")
    apply_parser.add_argument("--actor", default="local_operator")
    apply_parser.add_argument("--backup-dir", default=None)
    apply_parser.set_defaults(handler=apply_command)

    rollback = subcommands.add_parser("rollback", help="restore a recorded config backup")
    rollback.add_argument("backup_id")
    rollback.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    rollback.add_argument("--home", default=None, help="override home for default state path")
    rollback.add_argument("--actor", default="local_operator")
    rollback.set_defaults(handler=rollback_command)

    config = subcommands.add_parser("config", help="inspect config layout and policy")
    config_subcommands = config.add_subparsers(dest="config_command")
    inspect = config_subcommands.add_parser("inspect", help="inspect config paths and policy")
    inspect.add_argument("--home", default=None, help="override home directory for inspection")
    inspect.set_defaults(handler=config_inspect_command)

    discover = config_subcommands.add_parser(
        "discover", help="discover existing first-wave agent config files"
    )
    discover.add_argument("--home", default=None, help="override home directory for discovery")
    discover.add_argument(
        "--agents",
        default=None,
        help="comma-separated agent kinds to discover; defaults to all first-wave agents",
    )
    discover.set_defaults(handler=config_discover_command)

    approval = subcommands.add_parser("approval", help="list and decide approval tasks")
    approval_subcommands = approval.add_subparsers(dest="approval_command")
    approval_list = approval_subcommands.add_parser("list", help="list approval tasks")
    approval_list.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    approval_list.add_argument("--home", default=None, help="override home for default state path")
    approval_list.add_argument("--state", default=None, help="filter by approval state")
    approval_list.add_argument("--plan-id", default=None, help="filter by remediation plan id")
    approval_list.set_defaults(handler=approval_list_command)

    approval_approve = approval_subcommands.add_parser("approve", help="approve a pending task")
    approval_approve.add_argument("approval_id")
    approval_approve.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    approval_approve.add_argument(
        "--home", default=None, help="override home for default state path"
    )
    approval_approve.add_argument("--actor", default="local_operator")
    approval_approve.add_argument("--comment", default=None)
    approval_approve.set_defaults(handler=approval_approve_command)

    approval_reject = approval_subcommands.add_parser("reject", help="reject a pending task")
    approval_reject.add_argument("approval_id")
    approval_reject.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    approval_reject.add_argument(
        "--home", default=None, help="override home for default state path"
    )
    approval_reject.add_argument("--actor", default="local_operator")
    approval_reject.add_argument("--comment", default=None)
    approval_reject.set_defaults(handler=approval_reject_command)

    catalog = subcommands.add_parser("catalog", help="inspect catalog state")
    catalog_subcommands = catalog.add_subparsers(dest="catalog_command")
    catalog_list = catalog_subcommands.add_parser("list", help="list catalog entries")
    catalog_list.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    catalog_list.add_argument("--home", default=None, help="override home for default state path")
    catalog_list.set_defaults(handler=catalog_list_command)
    catalog_candidates = catalog_subcommands.add_parser(
        "candidates", help="list staged catalog candidates"
    )
    catalog_candidates.add_argument("--db-path", default=None, help="path to state db")
    catalog_candidates.add_argument(
        "--home", default=None, help="override home for default state path"
    )
    catalog_candidates.set_defaults(handler=catalog_candidates_command)
    catalog_review = catalog_subcommands.add_parser(
        "review",
        help="update catalog review/lifecycle state after operator review",
    )
    catalog_review.add_argument("catalog_id")
    catalog_review.add_argument(
        "--review-state",
        required=True,
        choices=["approved", "pending", "rejected", "quarantined"],
    )
    catalog_review.add_argument(
        "--lifecycle-state",
        default=None,
        choices=["enabled", "disabled", "deprecated"],
    )
    catalog_review.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    catalog_review.add_argument("--home", default=None, help="override home for default state path")
    catalog_review.add_argument("--actor", default="local_operator")
    catalog_review.add_argument("--comment", default=None)
    catalog_review.set_defaults(handler=catalog_review_command)
    catalog_review_legacy = catalog_subcommands.add_parser(
        "review-legacy-import",
        help="bulk-review catalog entries imported from legacy MCP Hub",
    )
    catalog_review_legacy.add_argument(
        "--review-state",
        default="approved",
        choices=["approved", "pending", "rejected", "quarantined"],
    )
    catalog_review_legacy.add_argument(
        "--lifecycle-state",
        default="enabled",
        choices=["enabled", "disabled", "deprecated"],
    )
    catalog_review_legacy.add_argument("--apply", action="store_true")
    catalog_review_legacy.add_argument(
        "--db-path", default=None, help="path to Multiplex SQLite state"
    )
    catalog_review_legacy.add_argument(
        "--home", default=None, help="override home for default state path"
    )
    catalog_review_legacy.add_argument("--actor", default="local_operator")
    catalog_review_legacy.add_argument("--comment", default=None)
    catalog_review_legacy.set_defaults(handler=catalog_review_legacy_import_command)

    runtime = subcommands.add_parser("runtime", help="inspect runtime state")
    runtime_subcommands = runtime.add_subparsers(dest="runtime_command")
    runtime_ps = runtime_subcommands.add_parser("ps", help="list runtime sessions")
    runtime_ps.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    runtime_ps.add_argument("--home", default=None, help="override home for default state path")
    runtime_ps.set_defaults(handler=runtime_ps_command)
    runtime_why_slow = runtime_subcommands.add_parser(
        "why-slow", help="explain runtime sharing/isolation"
    )
    runtime_why_slow.add_argument("--server", default=None)
    runtime_why_slow.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    runtime_why_slow.add_argument(
        "--home", default=None, help="override home for default state path"
    )
    runtime_why_slow.set_defaults(handler=runtime_why_slow_command)

    cutover = subcommands.add_parser("cutover", help="plan MCP Hub retirement workflows")
    cutover_subcommands = cutover.add_subparsers(dest="cutover_command")
    cutover_dry_run = cutover_subcommands.add_parser(
        "dry-run",
        help="inspect legacy MCP Hub state without mutating files or Multiplex state",
    )
    cutover_dry_run.add_argument(
        "--from",
        dest="source",
        required=True,
        help="cutover source; currently only mcp-hub is supported",
    )
    cutover_dry_run.add_argument(
        "--legacy-root",
        default=None,
        help="legacy home/config root to inspect read-only; auto-discovered when omitted",
    )
    cutover_dry_run.add_argument("--home", default=None, help="override home for discovery roots")
    cutover_dry_run.set_defaults(handler=cutover_dry_run_command)
    cutover_import_catalog = cutover_subcommands.add_parser(
        "import-catalog",
        help="normalize legacy MCP Hub catalog entries into Multiplex catalog state",
    )
    cutover_import_catalog.add_argument(
        "--from",
        dest="source",
        required=True,
        help="cutover source; currently only mcp-hub is supported",
    )
    cutover_import_catalog.add_argument(
        "--catalog-path",
        default=None,
        help="legacy MCP Hub catalog export JSON to normalize; auto-discovered when omitted",
    )
    cutover_import_catalog.add_argument(
        "--apply",
        action="store_true",
        help="write normalized entries to Multiplex catalog; default is dry-run",
    )
    cutover_import_catalog.add_argument(
        "--sample-limit",
        default=20,
        type=int,
        help="maximum entries/errors/warnings to emit in default summarized output",
    )
    cutover_import_catalog.add_argument(
        "--full-entries",
        action="store_true",
        help="emit the full normalized entry list instead of bounded samples",
    )
    cutover_import_catalog.add_argument(
        "--db-path", default=None, help="path to Multiplex SQLite state"
    )
    cutover_import_catalog.add_argument(
        "--home", default=None, help="override home for default state path"
    )
    cutover_import_catalog.add_argument("--actor", default="local_operator")
    cutover_import_catalog.set_defaults(handler=cutover_import_catalog_command)
    cutover_apply = cutover_subcommands.add_parser(
        "apply",
        help="record MCP Hub retirement after the global release gate passes",
    )
    cutover_apply.add_argument(
        "--from",
        dest="source",
        required=True,
        help="cutover source; currently only mcp-hub is supported",
    )
    cutover_apply.add_argument(
        "--confirm-retire-mcp-hub",
        action="store_true",
        help="explicitly confirm audited MCP Hub retirement",
    )
    cutover_apply.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    cutover_apply.add_argument("--home", default=None, help="override home for default state path")
    cutover_apply.add_argument(
        "--checkpoint-dir",
        default=DEFAULT_CERTIFICATION_DIR,
        help="directory containing real-client certification transcripts",
    )
    cutover_apply.add_argument(
        "--daemon-unit-dir",
        default=None,
        help="override systemd user unit directory for global cutover checks",
    )
    cutover_apply.add_argument(
        "--daemon-systemctl-bin",
        default="systemctl",
        help="systemctl executable to query for global cutover checks",
    )
    cutover_apply.add_argument("--actor", default="local_operator")
    cutover_apply.set_defaults(handler=cutover_apply_command)
    cutover_status = cutover_subcommands.add_parser(
        "status",
        help="report audited MCP Hub retirement status",
    )
    cutover_status.add_argument(
        "--from",
        dest="source",
        default="mcp-hub",
        help="cutover source; currently only mcp-hub is supported",
    )
    cutover_status.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    cutover_status.add_argument("--home", default=None, help="override home for default state path")
    cutover_status.add_argument(
        "--check-gate",
        action="store_true",
        help="also run the current global release gate",
    )
    cutover_status.add_argument(
        "--check-footprint",
        action="store_true",
        help="also inspect remaining legacy MCP Hub filesystem/service/process footprint",
    )
    cutover_status.add_argument(
        "--checkpoint-dir",
        default=DEFAULT_CERTIFICATION_DIR,
        help="directory containing real-client certification transcripts",
    )
    cutover_status.add_argument(
        "--daemon-unit-dir",
        default=None,
        help="override systemd user unit directory for global cutover checks",
    )
    cutover_status.add_argument(
        "--daemon-systemctl-bin",
        default="systemctl",
        help="systemctl executable to query for global cutover checks",
    )
    cutover_status.add_argument(
        "--legacy-root",
        default="~/mcp-hub",
        help="legacy MCP Hub repository/root to inspect when --check-footprint is used",
    )
    cutover_status.add_argument(
        "--ps-bin",
        default="ps",
        help="process listing executable for --check-footprint",
    )
    cutover_status.add_argument(
        "--no-processes",
        action="store_true",
        help="skip process inspection during --check-footprint",
    )
    cutover_status.set_defaults(handler=cutover_status_command)
    cutover_footprint = cutover_subcommands.add_parser(
        "legacy-footprint",
        help="report remaining legacy MCP Hub repo/service/process footprint",
    )
    cutover_footprint.add_argument(
        "--from",
        dest="source",
        default="mcp-hub",
        help="cutover source; currently only mcp-hub is supported",
    )
    cutover_footprint.add_argument(
        "--legacy-root",
        default="~/mcp-hub",
        help="legacy MCP Hub repository/root to inspect read-only",
    )
    cutover_footprint.add_argument(
        "--home",
        default=None,
        help="home directory for legacy systemd user unit candidates",
    )
    cutover_footprint.add_argument(
        "--ps-bin",
        default="ps",
        help="process listing executable for read-only legacy process detection",
    )
    cutover_footprint.add_argument(
        "--no-processes",
        action="store_true",
        help="skip process inspection and report only filesystem/service footprint",
    )
    cutover_footprint.set_defaults(handler=cutover_legacy_footprint_command)
    cutover_cleanup = cutover_subcommands.add_parser(
        "legacy-cleanup-plan",
        help="plan explicit operator cleanup for remaining legacy MCP Hub footprint",
    )
    cutover_cleanup.add_argument(
        "--from",
        dest="source",
        default="mcp-hub",
        help="cutover source; currently only mcp-hub is supported",
    )
    cutover_cleanup.add_argument(
        "--legacy-root",
        default="~/mcp-hub",
        help="legacy MCP Hub repository/root to inspect read-only",
    )
    cutover_cleanup.add_argument(
        "--home",
        default=None,
        help="home directory for legacy systemd user unit candidates",
    )
    cutover_cleanup.add_argument(
        "--ps-bin",
        default="ps",
        help="process listing executable for read-only legacy process detection",
    )
    cutover_cleanup.add_argument(
        "--no-processes",
        action="store_true",
        help="skip process inspection and plan only filesystem/service cleanup",
    )
    cutover_cleanup.set_defaults(handler=cutover_legacy_cleanup_plan_command)
    cutover_locate = cutover_subcommands.add_parser(
        "locate",
        help="locate likely legacy MCP Hub roots and catalog exports",
    )
    cutover_locate.add_argument("--home", default=None, help="override home for discovery roots")
    cutover_locate.set_defaults(handler=cutover_locate_command)

    agents = subcommands.add_parser("agents", help="manage agent registrations and installs")
    agents_subcommands = agents.add_subparsers(dest="agents_command")
    install_control = agents_subcommands.add_parser(
        "install-control-plane",
        help="install authenticated mcp_hub for a supported agent",
    )
    install_control.add_argument(
        "--agent",
        required=True,
        choices=["codex", "claude_code", "gemini", "cline", "opencode"],
        help="first-wave agent kind to configure",
    )
    install_control.add_argument(
        "--apply",
        action="store_true",
        help="mutate the target config after backup; default is dry-run",
    )
    install_control.add_argument(
        "--emit-token",
        action="store_true",
        help="include the one-time raw token in JSON output; use only in a controlled shell",
    )
    install_control.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    install_control.add_argument("--home", default=None, help="override home/config path root")
    install_control.add_argument(
        "--config-path",
        default=None,
        help="explicit agent config path to install into",
    )
    install_control.add_argument(
        "--helper-path",
        default=None,
        help="explicit Claude Code headersHelper script path",
    )
    install_control.add_argument("--backup-dir", default=None)
    install_control.add_argument("--actor", default="local_operator")
    install_control.set_defaults(handler=agents_install_control_plane_command)
    auth_capabilities = agents_subcommands.add_parser(
        "auth-capabilities",
        help="list first-wave control-plane auth install safety status",
    )
    auth_capabilities.set_defaults(handler=agents_auth_capabilities_command)
    agents_sync = agents_subcommands.add_parser(
        "sync",
        help="discover first-wave agent configs and sync them into Multiplex state",
    )
    agents_sync.add_argument(
        "--agent",
        default=None,
        choices=["codex", "claude_code", "gemini", "cline", "opencode"],
        help="sync only one first-wave agent kind",
    )
    agents_sync.add_argument("--apply", action="store_true", help="persist discovered agent paths")
    agents_sync.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    agents_sync.add_argument(
        "--home",
        default=None,
        help="override home for discovery and state path",
    )
    agents_sync.set_defaults(handler=agents_sync_command)
    agents_self_check = agents_subcommands.add_parser(
        "self-check",
        help="report observed mcp_hub control-plane readiness for registered agents",
    )
    agents_self_check.add_argument(
        "--agent",
        default=None,
        choices=["codex", "claude_code", "gemini", "cline", "opencode"],
        help="filter to one first-wave agent kind",
    )
    agents_self_check.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    agents_self_check.add_argument(
        "--home", default=None, help="override home for default state path"
    )
    agents_self_check.set_defaults(handler=agents_self_check_command)

    tui = subcommands.add_parser("tui", help="render the operator TUI")
    tui.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    tui.add_argument("--home", default=None, help="override home for default state path")
    tui.add_argument(
        "--approve",
        default=None,
        metavar="APPROVAL_ID",
        help="approve a pending task through the TUI operator channel",
    )
    tui.add_argument(
        "--repl",
        action="store_true",
        help="run the interactive operator REPL",
    )
    tui.add_argument(
        "--legacy-root",
        default=None,
        help="legacy MCP Hub root used by the cutover TUI view",
    )
    tui.add_argument(
        "--no-processes",
        action="store_true",
        help="skip process inspection in the cutover TUI view",
    )
    tui.add_argument("--actor", default="local_operator")
    tui.add_argument("--comment", default=None)
    tui.set_defaults(handler=tui_command)

    certify = subcommands.add_parser("certify", help="run real-client certification")
    certify_subcommands = certify.add_subparsers(dest="certify_command")
    certify_codex = certify_subcommands.add_parser("codex", help="certify Codex CLI")
    certify_codex.add_argument("--work-dir", default=None, help="isolated certification work dir")
    certify_codex.add_argument("--codex-bin", default="codex", help="Codex CLI binary")
    certify_codex.add_argument(
        "--transcript",
        default=None,
        help="write redacted markdown transcript to this path",
    )
    certify_codex.set_defaults(handler=certify_codex_command)
    certify_claude = certify_subcommands.add_parser("claude-code", help="certify Claude Code")
    certify_claude.add_argument("--work-dir", default=None, help="isolated certification work dir")
    certify_claude.add_argument("--claude-bin", default="claude", help="Claude Code binary")
    certify_claude.add_argument(
        "--transcript",
        default=None,
        help="write redacted markdown transcript to this path",
    )
    certify_claude.set_defaults(handler=certify_claude_code_command)
    certify_gemini = certify_subcommands.add_parser("gemini", help="certify Gemini CLI")
    certify_gemini.add_argument("--work-dir", default=None, help="isolated certification work dir")
    certify_gemini.add_argument("--gemini-bin", default="gemini", help="Gemini CLI binary")
    certify_gemini.add_argument(
        "--transcript",
        default=None,
        help="write redacted markdown transcript to this path",
    )
    certify_gemini.set_defaults(handler=certify_gemini_command)
    certify_cline = certify_subcommands.add_parser("cline", help="certify Cline")
    certify_cline.add_argument("--work-dir", default=None, help="isolated certification work dir")
    certify_cline.add_argument("--cline-bin", default="cline", help="Cline CLI binary")
    certify_cline.add_argument(
        "--transcript",
        default=None,
        help="write redacted markdown transcript to this path",
    )
    certify_cline.set_defaults(handler=certify_cline_command)
    certify_opencode = certify_subcommands.add_parser("opencode", help="certify OpenCode")
    certify_opencode.add_argument(
        "--work-dir",
        default=None,
        help="isolated certification work dir",
    )
    certify_opencode.add_argument(
        "--opencode-bin",
        default="opencode",
        help="OpenCode CLI binary",
    )
    certify_opencode.add_argument(
        "--transcript",
        default=None,
        help="write redacted markdown transcript to this path",
    )
    certify_opencode.set_defaults(handler=certify_opencode_command)
    certify_import = certify_subcommands.add_parser(
        "import-evidence",
        help="record certification transcript hashes in the audit chain",
    )
    certify_import.add_argument(
        "--checkpoint-dir",
        default=DEFAULT_CERTIFICATION_DIR,
        help="directory containing real-client certification transcripts",
    )
    certify_import.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    certify_import.add_argument("--home", default=None, help="override home for default state path")
    certify_import.add_argument("--actor", default="local_operator")
    certify_import.set_defaults(handler=certify_import_evidence_command)

    doctor = subcommands.add_parser("doctor", help="run local diagnostics")
    doctor_subcommands = doctor.add_subparsers(dest="doctor_command")
    release_gate = doctor_subcommands.add_parser("release-gate", help="run release gate checks")
    release_gate.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    release_gate.add_argument("--home", default=None, help="override home for default state path")
    release_gate.add_argument(
        "--checkpoint-dir",
        default=DEFAULT_CERTIFICATION_DIR,
        help="directory containing real-client certification transcripts",
    )
    release_gate.add_argument(
        "--global-cutover",
        action="store_true",
        help="require daemon service and legacy catalog readiness for MCP Hub retirement",
    )
    release_gate.add_argument(
        "--daemon-unit-dir",
        default=None,
        help="override systemd user unit directory for global cutover checks",
    )
    release_gate.add_argument(
        "--daemon-systemctl-bin",
        default="systemctl",
        help="systemctl executable to query for global cutover checks",
    )
    release_gate.set_defaults(handler=doctor_release_gate_command)
    retirement_gate = doctor_subcommands.add_parser(
        "retirement-gate",
        help="run final MCP Hub retirement completion checks",
    )
    retirement_gate.add_argument("--db-path", default=None, help="path to Multiplex SQLite state")
    retirement_gate.add_argument(
        "--home", default=None, help="override home for default state path"
    )
    retirement_gate.add_argument(
        "--checkpoint-dir",
        default=DEFAULT_CERTIFICATION_DIR,
        help="directory containing real-client certification transcripts",
    )
    retirement_gate.add_argument(
        "--daemon-unit-dir",
        default=None,
        help="override systemd user unit directory for global cutover checks",
    )
    retirement_gate.add_argument(
        "--daemon-systemctl-bin",
        default="systemctl",
        help="systemctl executable to query for global cutover checks",
    )
    retirement_gate.add_argument(
        "--legacy-root",
        default="~/mcp-hub",
        help="legacy MCP Hub repository/root to inspect read-only",
    )
    retirement_gate.add_argument(
        "--ps-bin",
        default="ps",
        help="process listing executable for read-only legacy process detection",
    )
    retirement_gate.add_argument(
        "--no-processes",
        action="store_true",
        help="skip process inspection during legacy footprint checks",
    )
    retirement_gate.set_defaults(handler=doctor_retirement_gate_command)
    migration_dry_run = doctor_subcommands.add_parser(
        "migration-dry-run",
        help="dry-run legacy config import without mutating files or state",
    )
    migration_dry_run.add_argument(
        "--legacy-root",
        required=True,
        help="legacy home/config root to inspect read-only",
    )
    migration_dry_run.set_defaults(handler=doctor_migration_dry_run_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the MCP Multiplex CLI."""
    parser = build_parser(prog=Path(sys.argv[0]).name or "mxp")
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stdout)
        return 0
    try:
        return int(handler(args, sys.stdout, sys.stderr))
    except BrokenPipeError:
        return 0
