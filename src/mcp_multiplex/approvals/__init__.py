"""Approval lifecycle storage and decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from mcp_multiplex.observability import EventStore
from mcp_multiplex.schemas import Approval
from mcp_multiplex.storage import migrate

DECISION_CHANNELS = frozenset({"cli", "tui"})
MODEL_ONLY_CHANNELS = frozenset({"control_mcp", "mcp_hub", "agent"})

ApprovalDecision = Literal["approved", "rejected"]


class ApprovalError(ValueError):
    """Raised when an approval transition is invalid or unsafe."""


@dataclass(frozen=True)
class PlanApprovalState:
    """Plan state needed to decide approval lifecycle transitions."""

    plan_id: str
    status: str
    agent_id: str | None
    target_path: str | None
    policy: dict[str, Any]

    @property
    def approval_required(self) -> bool:
        return self.policy.get("approval_required") is True


class ApprovalStore:
    """SQLite-backed approval lifecycle repository."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate(connection)

    def create_pending(
        self,
        plan_id: str,
        *,
        actor: str = "daemon",
        channel: str = "planner",
        created_at: str | None = None,
        expires_at: str | None = None,
        comment: str | None = None,
    ) -> Approval:
        """Create or return a pending approval task for an approval-required plan."""
        plan = self._plan(plan_id)
        if not plan.approval_required:
            raise ApprovalError("plan policy does not require approval")
        existing = self.find_by_plan(plan_id)
        if existing is not None:
            return existing
        approval = Approval.from_dict(
            {
                "schema_version": 1,
                "approval_id": approval_id_for_plan(plan_id),
                "plan_id": plan_id,
                "state": "pending",
                "actor": actor,
                "channel": channel,
                "created_at": created_at or _current_timestamp(),
                "expires_at": expires_at,
                "decision_at": None,
                "comment": comment,
            }
        )
        self._insert(approval)
        return approval

    def create_not_required(
        self,
        plan_id: str,
        *,
        actor: str = "daemon",
        channel: str = "planner",
        created_at: str | None = None,
    ) -> Approval:
        """Record that a plan did not require operator approval."""
        plan = self._plan(plan_id)
        if plan.approval_required:
            raise ApprovalError("approval-required plan cannot be marked not_required")
        existing = self.find_by_plan(plan_id)
        if existing is not None:
            return existing
        approval = Approval.from_dict(
            {
                "schema_version": 1,
                "approval_id": approval_id_for_plan(plan_id),
                "plan_id": plan_id,
                "state": "not_required",
                "actor": actor,
                "channel": channel,
                "created_at": created_at or _current_timestamp(),
                "expires_at": None,
                "decision_at": None,
                "comment": None,
            }
        )
        self._insert(approval)
        return approval

    def approve(
        self,
        approval_id: str,
        *,
        actor: str,
        channel: str,
        decided_at: str | None = None,
        comment: str | None = None,
    ) -> Approval:
        """Approve a pending approval through an operator channel."""
        return self._decide(
            approval_id,
            decision="approved",
            actor=actor,
            channel=channel,
            decided_at=decided_at,
            comment=comment,
        )

    def reject(
        self,
        approval_id: str,
        *,
        actor: str,
        channel: str,
        decided_at: str | None = None,
        comment: str | None = None,
    ) -> Approval:
        """Reject a pending approval through an operator channel."""
        return self._decide(
            approval_id,
            decision="rejected",
            actor=actor,
            channel=channel,
            decided_at=decided_at,
            comment=comment,
        )

    def show(self, approval_id: str) -> Approval:
        """Return one approval by id."""
        row = self.connection.execute(
            """
            SELECT *
            FROM approvals
            WHERE approval_id = ?
            """,
            (approval_id,),
        ).fetchone()
        if row is None:
            raise KeyError(approval_id)
        return _approval_from_row(row)

    def find_by_plan(self, plan_id: str) -> Approval | None:
        """Return the approval for a plan, if present."""
        row = self.connection.execute(
            """
            SELECT approval_id
            FROM approvals
            WHERE plan_id = ?
            ORDER BY created_at, approval_id
            LIMIT 1
            """,
            (plan_id,),
        ).fetchone()
        if row is None:
            return None
        return self.show(str(row["approval_id"]))

    def list(
        self,
        *,
        state: str | None = None,
        plan_id: str | None = None,
    ) -> list[Approval]:
        """List approvals in deterministic operator order."""
        clauses: list[str] = []
        params: list[str] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if plan_id is not None:
            clauses.append("plan_id = ?")
            params.append(plan_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT approval_id
            FROM approvals
            {where}
            ORDER BY
              CASE state
                WHEN 'pending' THEN 0
                WHEN 'approved' THEN 1
                WHEN 'rejected' THEN 2
                ELSE 3
              END,
              created_at,
              approval_id
            """,
            params,
        ).fetchall()
        return [self.show(str(row["approval_id"])) for row in rows]

    def plan_status(self, plan_id: str) -> str:
        """Return the current remediation plan status."""
        return self._plan(plan_id).status

    def _decide(
        self,
        approval_id: str,
        *,
        decision: ApprovalDecision,
        actor: str,
        channel: str,
        decided_at: str | None,
        comment: str | None,
    ) -> Approval:
        if channel in MODEL_ONLY_CHANNELS:
            raise ApprovalError("model-only approval is insufficient for destructive operations")
        if channel not in DECISION_CHANNELS:
            raise ApprovalError(f"unsupported approval decision channel: {channel}")
        existing = self.show(approval_id)
        if existing.state != "pending":
            raise ApprovalError(f"approval {approval_id} is not pending")
        plan = self._plan(existing.plan_id)
        if not plan.approval_required:
            raise ApprovalError("plan does not require approval")
        decision_time = decided_at or _current_timestamp()
        decided = Approval.from_dict(
            {
                **existing.to_dict(),
                "state": decision,
                "actor": actor,
                "channel": channel,
                "decision_at": decision_time,
                "comment": comment,
            }
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE approvals
                SET state = ?,
                    actor = ?,
                    channel = ?,
                    decision_at = ?,
                    comment = ?
                WHERE approval_id = ?
                """,
                (
                    decided.state,
                    decided.actor,
                    decided.channel,
                    decided.decision_at,
                    decided.comment,
                    decided.approval_id,
                ),
            )
            self.connection.execute(
                """
                UPDATE remediation_plans
                SET status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE plan_id = ?
                """,
                (decision, decided.plan_id),
            )
        EventStore(self.connection).append(
            event_id=_event_id(decided.approval_id, f"approval.{decision}", decision_time),
            event_type=f"approval.{decision}",
            actor=actor,
            agent_id=plan.agent_id,
            plan_id=plan.plan_id,
            target_path=plan.target_path,
            result="success",
            payload={
                "approval_id": decided.approval_id,
                "state": decided.state,
                "channel": channel,
                "comment": comment,
            },
            timestamp=decision_time,
        )
        return self.show(decided.approval_id)

    def _insert(self, approval: Approval) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO approvals (
                  approval_id,
                  schema_version,
                  plan_id,
                  state,
                  actor,
                  channel,
                  created_at,
                  expires_at,
                  decision_at,
                  comment
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.approval_id,
                    approval.schema_version,
                    approval.plan_id,
                    approval.state,
                    approval.actor,
                    approval.channel,
                    approval.created_at,
                    approval.expires_at,
                    approval.decision_at,
                    approval.comment,
                ),
            )

    def _plan(self, plan_id: str) -> PlanApprovalState:
        row = self.connection.execute(
            """
            SELECT plan_id, status, agent_id, target_path, policy_json
            FROM remediation_plans
            WHERE plan_id = ?
            """,
            (plan_id,),
        ).fetchone()
        if row is None:
            raise ApprovalError(f"unknown plan: {plan_id}")
        return PlanApprovalState(
            plan_id=str(row["plan_id"]),
            status=str(row["status"]),
            agent_id=row["agent_id"],
            target_path=row["target_path"],
            policy=json.loads(str(row["policy_json"])),
        )


def approval_id_for_plan(plan_id: str) -> str:
    """Return the deterministic approval id for a remediation plan."""
    digest = hashlib.sha256(plan_id.encode()).hexdigest()[:24]
    return f"appr_{digest}"


def _approval_from_row(row: sqlite3.Row) -> Approval:
    return Approval.from_dict(
        {
            "schema_version": int(row["schema_version"]),
            "approval_id": str(row["approval_id"]),
            "plan_id": str(row["plan_id"]),
            "state": str(row["state"]),
            "actor": str(row["actor"]),
            "channel": str(row["channel"]),
            "created_at": str(row["created_at"]),
            "expires_at": row["expires_at"],
            "decision_at": row["decision_at"],
            "comment": row["comment"],
        }
    )


def _event_id(approval_id: str, event_type: str, timestamp: str) -> str:
    digest = hashlib.sha256(f"{approval_id}\0{event_type}\0{timestamp}".encode()).hexdigest()
    return f"evt_{digest[:24]}"


def _current_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "DECISION_CHANNELS",
    "MODEL_ONLY_CHANNELS",
    "ApprovalError",
    "ApprovalStore",
    "PlanApprovalState",
    "approval_id_for_plan",
]
