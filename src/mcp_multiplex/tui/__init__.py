"""Text-mode operator TUI surface.

The first TUI surface is deterministic and scriptable so it can be smoke-tested
without a terminal UI framework. It renders the operator views required by the
roadmap and keeps mutation behind explicit approval actions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from mcp_multiplex.apply import ConfigBackupStore
from mcp_multiplex.approvals import ApprovalStore
from mcp_multiplex.catalog import CatalogCandidateStore, plan_legacy_mcp_hub_catalog_import
from mcp_multiplex.cli import (
    _agent_onboarding_payload,
    _legacy_cleanup_plan_payload,
    _legacy_mcp_hub_footprint_payload,
    _legacy_source_locator,
    _plan_row,
    _runtime_sharing_explanation,
    status_payload,
)
from mcp_multiplex.credentials import CredentialRefStore, readiness_summary
from mcp_multiplex.observability import EventStore
from mcp_multiplex.runtime import RuntimeBackendStore
from mcp_multiplex.storage import migrate

CHANGE_EVENT_TYPES = {
    "approval.approved",
    "approval.rejected",
    "config.drift_detected",
    "config.observed",
    "remediation.applied",
    "remediation.failed",
    "rollback.completed",
}

REPL_COMMANDS = [
    {
        "name": "onboarding",
        "aliases": ["setup", "sync"],
        "description": "Show discovered configs, unregistered agents, and next steps.",
    },
    {
        "name": "dashboard",
        "aliases": ["status", "refresh"],
        "description": "Show compact Multiplex health and counts.",
    },
    {
        "name": "self-check",
        "aliases": ["agents", "agent"],
        "description": "Show registered agents and mcp_hub control-plane readiness.",
    },
    {
        "name": "cutover",
        "aliases": ["retirement", "cleanup"],
        "description": "Show MCP Hub retirement status and remaining cleanup plan.",
    },
    {
        "name": "problems",
        "aliases": ["blockers", "warnings"],
        "description": "Show blockers, warnings, and notices separately.",
    },
    {
        "name": "approvals",
        "aliases": ["approval"],
        "description": "Show approval tasks with reason, risk, diff, and rollback path.",
    },
    {
        "name": "approve",
        "aliases": [],
        "description": "Approve one pending approval: approve <approval-id>.",
    },
    {
        "name": "candidates",
        "aliases": ["candidate"],
        "description": "Show staged catalog candidates requiring review.",
    },
    {
        "name": "runtime",
        "aliases": ["why-slow"],
        "description": "Show runtime backends and why-slow sharing diagnostics.",
    },
    {
        "name": "credentials",
        "aliases": ["creds"],
        "description": "Show credential readiness without raw secret references.",
    },
    {
        "name": "rollback",
        "aliases": ["backups"],
        "description": "Show rollback plans and exact config backups.",
    },
    {
        "name": "changes",
        "aliases": ["what-changed"],
        "description": "Show recent audit events that changed or observed config state.",
    },
    {
        "name": "all",
        "aliases": ["show"],
        "description": "Render every TUI section.",
    },
    {
        "name": "commands",
        "aliases": ["help", "h", "?"],
        "description": "List available REPL slash commands.",
    },
    {
        "name": "quit",
        "aliases": ["q", "exit"],
        "description": "Exit the REPL.",
    },
]

COMMAND_ALIASES = {
    alias: command["name"]
    for command in REPL_COMMANDS
    for alias in [str(command["name"]), *[str(item) for item in command["aliases"]]]
}


@dataclass(frozen=True)
class TUIScreen:
    """Rendered TUI output plus structured state for tests and future adapters."""

    snapshot: dict[str, Any]
    text: str


def render_tui(
    connection: sqlite3.Connection,
    *,
    legacy_root: Path | None = None,
    home: Path | None = None,
    include_processes: bool = True,
) -> TUIScreen:
    """Return the complete operator TUI snapshot and text rendering."""
    migrate(connection)
    snapshot = tui_snapshot(
        connection,
        legacy_root=legacy_root,
        home=home,
        include_processes=include_processes,
    )
    return TUIScreen(snapshot=snapshot, text=render_snapshot(snapshot))


def approve_from_tui(
    connection: sqlite3.Connection,
    approval_id: str,
    *,
    actor: str = "local_operator",
    comment: str | None = None,
) -> dict[str, Any]:
    """Approve one pending approval through the TUI operator channel."""
    approval = ApprovalStore(connection).approve(
        approval_id,
        actor=actor,
        channel="tui",
        comment=comment,
    )
    return {
        "schema_version": 1,
        "kind": "MCPMultiplexTUIApprovalDecision",
        "ok": True,
        "approval": approval.to_dict(),
        "plan_status": ApprovalStore(connection).plan_status(approval.plan_id),
    }


def run_tui_repl(
    connection: sqlite3.Connection,
    *,
    stdin: TextIO,
    stdout: TextIO,
    actor: str = "local_operator",
    comment: str | None = None,
    legacy_root: Path | None = None,
    home: Path | None = None,
    include_processes: bool = True,
) -> int:
    """Run the scriptable operator REPL over the TUI snapshot."""
    stdout.write(_repl_header())
    stdout.write(_repl_help())
    while True:
        stdout.write("mxp> ")
        stdout.flush()
        line = stdin.readline()
        if line == "":
            stdout.write("\n")
            return 0
        command = line.strip()
        if not command:
            continue
        result = handle_repl_command(
            connection,
            command,
            actor=actor,
            comment=comment,
            legacy_root=legacy_root,
            home=home,
            include_processes=include_processes,
        )
        stdout.write(str(result["text"]))
        if result["exit"]:
            return int(result["code"])


def handle_repl_command(
    connection: sqlite3.Connection,
    command: str,
    *,
    actor: str = "local_operator",
    comment: str | None = None,
    legacy_root: Path | None = None,
    home: Path | None = None,
    include_processes: bool = True,
) -> dict[str, Any]:
    """Handle one REPL command and return text plus exit metadata."""
    normalized = command.strip()
    if normalized.startswith("/"):
        normalized = normalized[1:]
    parts = normalized.split()
    raw_name = parts[0].lower() if parts else "dashboard"
    name = COMMAND_ALIASES.get(raw_name, raw_name)
    args = parts[1:]
    snapshot = tui_snapshot(
        connection,
        legacy_root=legacy_root,
        home=home,
        include_processes=include_processes,
    )
    if name == "quit":
        return _repl_result("bye\n", exit=True)
    if name == "commands":
        return _repl_result(_repl_help())
    if name == "onboarding":
        return _repl_result(_section_text(snapshot, "onboarding"))
    if name == "dashboard":
        return _repl_result(_section_text(snapshot, "dashboard"))
    if name == "all":
        return _repl_result(render_snapshot(snapshot))
    if name == "self-check":
        return _repl_result(_section_text(snapshot, "agents"))
    if name == "cutover":
        return _repl_result(_section_text(snapshot, "cutover"))
    if name == "problems":
        return _repl_result(_section_text(snapshot, "problems"))
    if name == "approvals":
        return _repl_result(_section_text(snapshot, "approvals"))
    if name == "candidates":
        return _repl_result(_section_text(snapshot, "candidates"))
    if name == "runtime":
        return _repl_result(_section_text(snapshot, "runtime"))
    if name == "credentials":
        return _repl_result(_section_text(snapshot, "credentials"))
    if name == "rollback":
        return _repl_result(_section_text(snapshot, "rollback"))
    if name == "changes":
        return _repl_result(_section_text(snapshot, "what_changed"))
    if name == "approve":
        if not args:
            return _repl_result("usage: approve <approval-id>\n", code=2)
        try:
            payload = approve_from_tui(connection, args[0], actor=actor, comment=comment)
        except Exception as error:  # noqa: BLE001 - surfaced to operator, no hidden failure.
            return _repl_result(f"approval failed: {error}\n", code=2)
        approval = payload["approval"]
        return _repl_result(
            f"approved {approval['approval_id']} plan={approval['plan_id']} "
            f"status={payload['plan_status']}\n"
        )
    return _repl_result(f"unknown command: {command}\n" + _repl_help(), code=2)


def tui_snapshot(
    connection: sqlite3.Connection,
    *,
    legacy_root: Path | None = None,
    home: Path | None = None,
    include_processes: bool = True,
) -> dict[str, Any]:
    """Build the structured state behind the TUI views."""
    status = status_payload(connection)
    approvals = _approval_rows(connection, state="pending")
    approval_history = _approval_rows(connection)
    candidates = [candidate.to_dict() for candidate in CatalogCandidateStore(connection).list()]
    backends = [backend.to_dict() for backend in RuntimeBackendStore(connection).list()]
    return {
        "schema_version": 1,
        "kind": "MCPMultiplexTUI",
        "dashboard": {
            "ok": status["ok"],
            "summary": status["summary"],
        },
        "onboarding": _agent_onboarding_payload(connection, home=home),
        "problems": {
            "blockers": status["blockers"],
            "warnings": status["warnings"],
            "notices": status["notices"],
        },
        "agents": _agent_self_checks(connection),
        "cutover": _cutover_view(
            connection,
            legacy_root=legacy_root,
            home=home,
            include_processes=include_processes,
        ),
        "approvals": approvals,
        "candidates": candidates,
        "runtime": {
            "backends": backends,
            "why_slow": [_runtime_diagnostic(backend) for backend in backends],
        },
        "credentials": _credential_readiness(connection),
        "rollback": {
            "backups": [backup.to_dict() for backup in ConfigBackupStore(connection).list()],
            "plans": [_rollback_plan_view(plan) for plan in approval_history if plan.get("plan")],
        },
        "what_changed": _what_changed(connection),
    }


def render_snapshot(snapshot: dict[str, Any]) -> str:
    """Render a TUI snapshot into operator-readable text."""
    lines: list[str] = []
    dashboard = snapshot["dashboard"]
    summary = dashboard["summary"]
    state = "OK" if dashboard["ok"] else "BLOCKED"
    lines.extend(
        [
            "MCP Multiplex TUI",
            "",
            "[Dashboard]",
            (
                f"State: {state} | Blockers: {summary['blockers']} | "
                f"Warnings: {summary['warnings']} | Notices: {summary['notices']} | "
                f"Pending approvals: {summary['pending_approvals']}"
            ),
        ]
    )
    _render_onboarding(lines, snapshot["onboarding"])
    _render_problems(lines, snapshot["problems"])
    _render_agents(lines, snapshot["agents"])
    _render_cutover(lines, snapshot["cutover"])
    _render_approvals(lines, snapshot["approvals"])
    _render_candidates(lines, snapshot["candidates"])
    _render_runtime(lines, snapshot["runtime"])
    _render_credentials(lines, snapshot["credentials"])
    _render_rollback(lines, snapshot["rollback"])
    _render_what_changed(lines, snapshot["what_changed"])
    return "\n".join(lines).rstrip() + "\n"


def _repl_header() -> str:
    return "MCP Multiplex\nlocal MCP control plane // governed catalog // runtime proxy\n\n"


def _repl_help() -> str:
    lines = ["Commands:"]
    for command in REPL_COMMANDS:
        aliases = ", ".join(str(alias) for alias in command["aliases"])
        suffix = f" aliases={aliases}" if aliases else ""
        lines.append(f"- /{command['name']}{suffix}: {command['description']}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _repl_result(text: str, *, exit: bool = False, code: int = 0) -> dict[str, Any]:
    return {"text": text, "exit": exit, "code": code}


def _section_text(snapshot: dict[str, Any], section: str) -> str:
    lines: list[str] = []
    if section == "dashboard":
        dashboard = snapshot["dashboard"]
        summary = dashboard["summary"]
        state = "OK" if dashboard["ok"] else "BLOCKED"
        lines.extend(
            [
                "[Dashboard]",
                (
                    f"State: {state} | Blockers: {summary['blockers']} | "
                    f"Warnings: {summary['warnings']} | Notices: {summary['notices']} | "
                    f"Pending approvals: {summary['pending_approvals']}"
                ),
            ]
        )
    elif section == "onboarding":
        _render_onboarding(lines, snapshot["onboarding"])
    elif section == "agents":
        _render_agents(lines, snapshot["agents"])
    elif section == "cutover":
        _render_cutover(lines, snapshot["cutover"])
    elif section == "problems":
        _render_problems(lines, snapshot["problems"])
    elif section == "approvals":
        _render_approvals(lines, snapshot["approvals"])
    elif section == "candidates":
        _render_candidates(lines, snapshot["candidates"])
    elif section == "runtime":
        _render_runtime(lines, snapshot["runtime"])
    elif section == "credentials":
        _render_credentials(lines, snapshot["credentials"])
    elif section == "rollback":
        _render_rollback(lines, snapshot["rollback"])
    elif section == "what_changed":
        _render_what_changed(lines, snapshot["what_changed"])
    else:
        lines.append(f"unknown section: {section}")
    return "\n".join(lines).strip() + "\n"


def _render_onboarding(lines: list[str], onboarding: dict[str, Any]) -> None:
    lines.extend(["", "[Onboarding]"])
    discovered = onboarding["discovered_config_paths"]
    if not discovered:
        lines.append("- no agent configs discovered")
    else:
        lines.append(f"- discovered config paths: {len(discovered)}")
        for item in discovered[:8]:
            lines.append(f"  {item['agent_kind']}: {item['path']}")
    if onboarding["unregistered_agents"]:
        lines.append(f"- unregistered discovered paths: {onboarding['unregistered_count']}")
        if onboarding["next_action"]:
            lines.append(f"  next: {onboarding['next_action']}")
    else:
        lines.append("- registration state is in sync")


def _approval_rows(
    connection: sqlite3.Connection,
    *,
    state: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for approval in ApprovalStore(connection).list(state=state):
        plan = _plan_row(connection, approval.plan_id)
        policy = plan["policy"] if isinstance(plan["policy"], dict) else {}
        rows.append(
            {
                **approval.to_dict(),
                "plan": {
                    "plan_id": plan["plan_id"],
                    "status": plan["status"],
                    "plan_type": plan["plan_type"],
                    "agent_id": plan["agent_id"],
                    "target_path": plan["target_path"],
                    "catalog_id": plan["catalog_id"],
                    "approval_reason": policy.get("approval_reason"),
                    "risk": plan["risk"],
                    "diff": plan["diff"],
                    "rollback_strategy": plan["rollback_strategy"],
                },
            }
        )
    return rows


def _credential_readiness(connection: sqlite3.Connection) -> dict[str, Any]:
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
    summary = readiness_summary(
        CredentialRefStore(connection).list(),
        active_catalog_ids=active_catalog_ids,
    )
    return summary.to_dict()


def _runtime_diagnostic(backend: dict[str, Any]) -> dict[str, Any]:
    pool_key = str(backend["runtime_pool_key"])
    return {
        "backend_id": backend["backend_id"],
        "catalog_id": backend["catalog_id"],
        "state": backend["state"],
        "runtime_pool_key": pool_key,
        "sharing_explanation": _runtime_sharing_explanation(pool_key),
    }


def _agent_self_checks(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    agents = connection.execute(
        """
        SELECT agent_id,
               agent_kind,
               display_name,
               certification_level,
               control_plane_mount
        FROM agents
        ORDER BY agent_kind, agent_id
        """
    ).fetchall()
    checks: list[dict[str, Any]] = []
    for agent in agents:
        config_paths = [
            {
                "path": str(row["path"]),
                "format": str(row["format"]),
                "precedence": int(row["precedence"]),
                "is_project_shared": bool(row["is_project_shared"]),
            }
            for row in connection.execute(
                """
                SELECT path, format, precedence, is_project_shared
                FROM agent_config_paths
                WHERE agent_id = ?
                ORDER BY precedence DESC, path
                """,
                (agent["agent_id"],),
            ).fetchall()
        ]
        hubs = [
            {
                "mount_name": str(row["mount_name"]),
                "enabled": bool(row["enabled"]),
                "transport": str(row["transport"]),
                "url": row["url"],
                "authenticated": "Authorization"
                in [str(item) for item in _json_list(row["headers_present_json"])],
                "config_path": str(row["config_path"]),
            }
            for row in connection.execute(
                """
                SELECT mount_name,
                       enabled,
                       transport,
                       url,
                       headers_present_json,
                       config_path
                FROM observed_entries
                WHERE agent_id = ?
                  AND mount_name = ?
                ORDER BY config_path, observed_entry_id
                """,
                (agent["agent_id"], agent["control_plane_mount"]),
            ).fetchall()
        ]
        ready_hubs = [
            hub
            for hub in hubs
            if hub["enabled"]
            and hub["transport"] == "streamable_http"
            and str(hub["url"]).endswith("/servers/mcp_hub/mcp")
            and hub["authenticated"]
        ]
        checks.append(
            {
                "agent_id": str(agent["agent_id"]),
                "agent_kind": str(agent["agent_kind"]),
                "display_name": str(agent["display_name"]),
                "certification_level": str(agent["certification_level"]),
                "control_plane_mount": str(agent["control_plane_mount"]),
                "config_paths": config_paths,
                "mcp_hub_entries": hubs,
                "self_check": "ready" if ready_hubs else "needs_install_or_audit",
            }
        )
    return checks


def _cutover_view(
    connection: sqlite3.Connection,
    *,
    legacy_root: Path | None,
    home: Path | None,
    include_processes: bool,
) -> dict[str, Any]:
    events = [
        record
        for record in EventStore(connection).query(event_type="cutover.applied")
        if record.payload.get("source") == "mcp-hub"
        and record.payload.get("legacy_mcp_hub_deprecated") is True
    ]
    latest = events[-1] if events else None
    located = _legacy_source_locator(home=home)
    import_summary = None
    selected_catalog_path = located["selected_catalog_path"]
    if selected_catalog_path:
        try:
            import_plan = plan_legacy_mcp_hub_catalog_import(Path(str(selected_catalog_path)))
        except Exception:  # noqa: BLE001 - summary is best-effort in the TUI.
            import_summary = None
        else:
            import_summary = import_plan.to_summary_dict(sample_limit=5)
    resolved_legacy_root = (
        legacy_root.expanduser().resolve()
        if legacy_root is not None
        else Path(str(located["selected_legacy_root"] or Path("~/mcp-hub").expanduser())).resolve()
    )
    footprint = _legacy_mcp_hub_footprint_payload(
        legacy_root=resolved_legacy_root,
        home=home,
        ps_bin="ps",
        include_processes=include_processes,
    )
    cleanup_plan = _legacy_cleanup_plan_payload(footprint)
    return {
        "ok": latest is not None and cleanup_plan["ok"] is True,
        "legacy_mcp_hub_deprecated": latest is not None,
        "latest_event_id": latest.event.event_id if latest is not None else None,
        "latest_event_hash": latest.event.event_hash if latest is not None else None,
        "legacy_footprint_ok": footprint["ok"],
        "legacy_footprint_actions": [
            action["kind"] for action in footprint["operator_actions_required"]
        ],
        "legacy_process_matches": footprint["process_scan"]["match_count"],
        "cleanup_step_count": cleanup_plan["step_count"],
        "located_legacy_root": located["selected_legacy_root"],
        "located_catalog_path": located["selected_catalog_path"],
        "import_summary": import_summary,
        "cleanup_steps": [
            {
                "step_id": step["step_id"],
                "approval_required": step["approval_required"],
                "destructive": step["destructive"],
                "reason": step["reason"],
            }
            for step in cleanup_plan["steps"]
        ],
        "apply_supported": cleanup_plan["apply_supported"],
        "mutation_action": cleanup_plan["mutation_action"],
        "unmanaged_process_action": cleanup_plan["unmanaged_process_action"],
    }


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value))
    except Exception:  # noqa: BLE001 - malformed observed metadata is rendered as absent.
        return []
    return parsed if isinstance(parsed, list) else []


def _rollback_plan_view(approval: dict[str, Any]) -> dict[str, Any]:
    plan = approval["plan"]
    return {
        "plan_id": plan["plan_id"],
        "status": plan["status"],
        "target_path": plan["target_path"],
        "rollback_strategy": plan["rollback_strategy"],
    }


def _what_changed(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    changes = []
    for record in EventStore(connection).query():
        event = record.event
        if event.event_type not in CHANGE_EVENT_TYPES:
            continue
        changes.append(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "actor": event.actor,
                "agent_id": event.agent_id,
                "plan_id": event.plan_id,
                "target_path": event.target_path,
                "backup_id": event.backup_id,
                "result": event.result,
                "timestamp": event.timestamp,
            }
        )
    return changes[-10:]


def _render_problems(lines: list[str], problems: dict[str, Any]) -> None:
    lines.extend(["", "[Problems]", "Blockers:"])
    _render_issue_list(lines, problems["blockers"])
    lines.append("Warnings:")
    _render_issue_list(lines, problems["warnings"])
    lines.append("Notices:")
    _render_issue_list(lines, problems["notices"])


def _render_issue_list(lines: list[str], issues: list[dict[str, Any]]) -> None:
    if not issues:
        lines.append("- none")
        return
    for issue in issues:
        agent = f" agent={issue['agent_id']}" if issue.get("agent_id") else ""
        server = f" server={issue['server']}" if issue.get("server") else ""
        lines.append(f"- {issue['code']}:{agent}{server} {issue['detail']}")


def _render_agents(lines: list[str], agents: list[dict[str, Any]]) -> None:
    lines.extend(["", "[Agents]"])
    if not agents:
        lines.append("- none")
        return
    for agent in agents:
        lines.append(
            f"- {agent['agent_kind']} {agent['agent_id']} "
            f"cert={agent['certification_level']} self_check={agent['self_check']}"
        )
        if not agent["config_paths"]:
            lines.append("  configs: none")
        for config_path in agent["config_paths"]:
            shared = " project-shared" if config_path["is_project_shared"] else ""
            lines.append(
                f"  config: {config_path['path']} "
                f"format={config_path['format']} precedence={config_path['precedence']}{shared}"
            )
        if not agent["mcp_hub_entries"]:
            lines.append("  mcp_hub: missing")
        for hub in agent["mcp_hub_entries"]:
            auth = "auth=yes" if hub["authenticated"] else "auth=no"
            enabled = "enabled" if hub["enabled"] else "disabled"
            lines.append(
                f"  mcp_hub: {enabled} transport={hub['transport']} {auth} url={hub['url']}"
            )


def _render_cutover(lines: list[str], cutover: dict[str, Any]) -> None:
    lines.extend(["", "[Cutover]"])
    state = "OK" if cutover["ok"] else "NEEDS_OPERATOR_CLEANUP"
    deprecated = "yes" if cutover["legacy_mcp_hub_deprecated"] else "no"
    footprint = "clean" if cutover["legacy_footprint_ok"] else "dirty"
    lines.append(
        f"- state={state} legacy_mcp_hub_deprecated={deprecated} "
        f"footprint={footprint} cleanup_steps={cutover['cleanup_step_count']}"
    )
    if cutover["located_legacy_root"]:
        lines.append(f"  located root: {cutover['located_legacy_root']}")
    if cutover["located_catalog_path"]:
        lines.append(f"  located catalog: {cutover['located_catalog_path']}")
    if cutover["import_summary"] is not None:
        summary = cutover["import_summary"]
        lines.append(
            f"  importable entries: {summary['entry_count']} "
            f"warnings: {len(summary['warnings'])}{'+' if summary['warnings_truncated'] else ''}"
        )
        if summary["warnings_by_code"]:
            ranked_codes = sorted(
                summary["warnings_by_code"].items(),
                key=lambda item: (-item[1], item[0]),
            )[:3]
            top_codes = ", ".join(
                f"{code}={count}"
                for code, count in ranked_codes
            )
            lines.append(f"  top warning buckets: {top_codes}")
    if cutover["latest_event_id"]:
        lines.append(
            f"  cutover_event={cutover['latest_event_id']} hash={cutover['latest_event_hash']}"
        )
    else:
        lines.append("  cutover_event=missing")
    if cutover["legacy_footprint_actions"]:
        lines.append("  footprint actions: " + ", ".join(cutover["legacy_footprint_actions"]))
    else:
        lines.append("  footprint actions: none")
    lines.append(f"  legacy process matches: {cutover['legacy_process_matches']}")
    if not cutover["cleanup_steps"]:
        lines.append("  cleanup: none")
    for step in cutover["cleanup_steps"]:
        approval = "approval=yes" if step["approval_required"] else "approval=no"
        destructive = "destructive=yes" if step["destructive"] else "destructive=no"
        lines.append(f"  cleanup: {step['step_id']} {approval} {destructive}")
        lines.append(f"    reason: {step['reason']}")
    lines.append(
        f"  apply_supported={cutover['apply_supported']} "
        f"mutation_action={cutover['mutation_action']} "
        f"unmanaged_process_action={cutover['unmanaged_process_action']}"
    )


def _render_approvals(lines: list[str], approvals: list[dict[str, Any]]) -> None:
    lines.extend(["", "[Approvals]"])
    if not approvals:
        lines.append("- none")
        return
    for approval in approvals:
        plan = approval["plan"]
        lines.append(
            f"- {approval['approval_id']} state={approval['state']} "
            f"plan={plan['plan_id']} target={plan['target_path']}"
        )
        if plan.get("approval_reason"):
            lines.append(f"  reason: {plan['approval_reason']}")
        lines.append(f"  risk: {_risk_summary(plan['risk'])}")
        lines.append("  diff:")
        for diff_line in str(plan["diff"]["text"]).splitlines() or ["<empty diff>"]:
            lines.append(f"    {diff_line}")
        lines.append(f"  rollback: {plan['rollback_strategy']}")


def _render_candidates(lines: list[str], candidates: list[dict[str, Any]]) -> None:
    lines.extend(["", "[Candidates]"])
    if not candidates:
        lines.append("- none")
        return
    for candidate in candidates:
        reasons = ", ".join(candidate["reasons"])
        lines.append(
            f"- {candidate['candidate_id']} {candidate['proposed_name']} "
            f"state={candidate['review_state']} risk={candidate['risk_tier']} reasons={reasons}"
        )


def _risk_summary(risk: dict[str, Any]) -> str:
    parts = [
        f"tier={risk.get('tier', 'unknown')}",
        f"verification={risk.get('verification', 'unknown')}",
        f"rollback={risk.get('rollback', 'unknown')}",
    ]
    reasons = risk.get("reasons")
    if isinstance(reasons, list) and reasons:
        parts.append("reasons=" + ", ".join(str(reason) for reason in reasons))
    return " ".join(parts)


def _render_runtime(lines: list[str], runtime: dict[str, Any]) -> None:
    lines.extend(["", "[Runtime]", "Backends:"])
    if not runtime["backends"]:
        lines.append("- none")
    for backend in runtime["backends"]:
        lines.append(
            f"- {backend['backend_id']} catalog={backend['catalog_id']} "
            f"state={backend['state']} pool={backend['runtime_pool_key']}"
        )
    lines.append("Why slow:")
    if not runtime["why_slow"]:
        lines.append("- none")
    for diagnostic in runtime["why_slow"]:
        lines.append(
            f"- {diagnostic['catalog_id']}: {diagnostic['sharing_explanation']} "
            f"(state={diagnostic['state']})"
        )


def _render_credentials(lines: list[str], credentials: dict[str, Any]) -> None:
    lines.extend(["", "[Credentials]", "Blockers:"])
    _render_credential_items(lines, credentials["blockers"])
    lines.append("Warnings:")
    _render_credential_items(lines, credentials["warnings"])
    lines.append("Notices:")
    _render_credential_items(lines, credentials["notices"])


def _render_credential_items(lines: list[str], items: list[dict[str, Any]]) -> None:
    if not items:
        lines.append("- none")
        return
    for item in items:
        lines.append(
            f"- {item['catalog_id']} {item['name']} "
            f"state={item['readiness_state']} code={item['code']}"
        )


def _render_rollback(lines: list[str], rollback: dict[str, Any]) -> None:
    lines.extend(["", "[Rollback]", "Plans:"])
    if not rollback["plans"]:
        lines.append("- none")
    for plan in rollback["plans"]:
        lines.append(
            f"- {plan['plan_id']} target={plan['target_path']} "
            f"strategy={plan['rollback_strategy']} status={plan['status']}"
        )
    lines.append("Backups:")
    if not rollback["backups"]:
        lines.append("- none")
    for backup in rollback["backups"]:
        restored = backup["restored_at"] or "not restored"
        lines.append(
            f"- {backup['backup_id']} plan={backup['plan_id']} "
            f"target={backup['target_path']} restored={restored}"
        )


def _render_what_changed(lines: list[str], changes: list[dict[str, Any]]) -> None:
    lines.extend(["", "[What Changed]"])
    if not changes:
        lines.append("- none")
        return
    for change in changes:
        target = f" target={change['target_path']}" if change.get("target_path") else ""
        plan = f" plan={change['plan_id']}" if change.get("plan_id") else ""
        backup = f" backup={change['backup_id']}" if change.get("backup_id") else ""
        lines.append(
            f"- {change['timestamp']} {change['event_type']} actor={change['actor']}"
            f"{plan}{target}{backup} result={change['result']}"
        )


__all__ = [
    "TUIScreen",
    "approve_from_tui",
    "handle_repl_command",
    "run_tui_repl",
    "render_snapshot",
    "render_tui",
    "tui_snapshot",
]
