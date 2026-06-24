"""Catalog candidate staging for unknown observed MCP entries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from mcp_multiplex.catalog import CatalogStore
from mcp_multiplex.catalog.matching import CatalogMatch, match_observed_entry_from_store
from mcp_multiplex.schemas import CatalogCandidate, ObservedEntry
from mcp_multiplex.storage import migrate

CandidateStageStatus = Literal["created", "existing", "matched_known", "unsupported"]


@dataclass(frozen=True)
class CandidateStageResult:
    """Result of attempting to stage an observed entry as a candidate."""

    status: CandidateStageStatus
    observed_entry_id: str
    match: CatalogMatch
    candidate: CatalogCandidate | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observed_entry_id": self.observed_entry_id,
            "match": self.match.to_dict(),
            "candidate": self.candidate.to_dict() if self.candidate is not None else None,
        }


class CatalogCandidateStore:
    """SQLite-backed catalog candidate repository."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate(connection)

    def upsert(self, candidate: CatalogCandidate) -> CatalogCandidate:
        """Insert or update one catalog candidate."""
        validated = CatalogCandidate.from_dict(candidate.to_dict())
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO catalog_candidates (
                  candidate_id,
                  schema_version,
                  source,
                  observed_entry_id,
                  proposed_name,
                  classification,
                  review_state,
                  risk_tier,
                  confidence,
                  backend_shape_json,
                  approval_required,
                  reasons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                  source = excluded.source,
                  observed_entry_id = excluded.observed_entry_id,
                  proposed_name = excluded.proposed_name,
                  classification = excluded.classification,
                  review_state = excluded.review_state,
                  risk_tier = excluded.risk_tier,
                  confidence = excluded.confidence,
                  backend_shape_json = excluded.backend_shape_json,
                  approval_required = excluded.approval_required,
                  reasons_json = excluded.reasons_json,
                  updated_at = CURRENT_TIMESTAMP
                """,
                _candidate_row(validated),
            )
        return self.show(validated.candidate_id)

    def show(self, candidate_id: str) -> CatalogCandidate:
        """Return one catalog candidate."""
        row = self.connection.execute(
            """
            SELECT *
            FROM catalog_candidates
            WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return _candidate_from_row(row)

    def list(self) -> list[CatalogCandidate]:
        """List candidates in deterministic review order."""
        rows = self.connection.execute(
            """
            SELECT candidate_id
            FROM catalog_candidates
            ORDER BY review_state, proposed_name, candidate_id
            """
        ).fetchall()
        return [self.show(str(row["candidate_id"])) for row in rows]


def stage_unknown_candidate(
    connection: sqlite3.Connection,
    observed_entry: ObservedEntry,
    catalog_store: CatalogStore | None = None,
) -> CandidateStageResult:
    """Stage unmatched observed stdio/local HTTP entries as pending candidates."""
    store = catalog_store or CatalogStore(connection)
    match = match_observed_entry_from_store(observed_entry, store)
    if match.matched:
        return CandidateStageResult(
            status="matched_known",
            observed_entry_id=observed_entry.observed_entry_id,
            match=match,
        )

    candidate = candidate_from_observed_entry(observed_entry)
    if candidate is None:
        return CandidateStageResult(
            status="unsupported",
            observed_entry_id=observed_entry.observed_entry_id,
            match=match,
        )
    candidate_store = CatalogCandidateStore(connection)
    status: CandidateStageStatus = (
        "existing" if _candidate_exists(connection, candidate.candidate_id) else "created"
    )
    stored = candidate_store.upsert(candidate)
    return CandidateStageResult(
        status=status,
        observed_entry_id=observed_entry.observed_entry_id,
        match=match,
        candidate=stored,
    )


def candidate_from_observed_entry(observed_entry: ObservedEntry) -> CatalogCandidate | None:
    """Build a pending candidate from an unmatched observed entry."""
    classification = candidate_classification(observed_entry)
    if classification is None:
        return None
    payload = {
        "schema_version": 1,
        "candidate_id": candidate_id(observed_entry),
        "source": "observed_agent_config",
        "observed_entry_id": observed_entry.observed_entry_id,
        "proposed_name": observed_entry.mount_name,
        "classification": classification,
        "review_state": "pending",
        "risk_tier": "unknown",
        "confidence": "low",
        "backend_shape": backend_shape(observed_entry),
        "approval_required": True,
        "reasons": candidate_reasons(observed_entry, classification),
    }
    return CatalogCandidate.from_dict(payload)


def candidate_classification(observed_entry: ObservedEntry) -> str | None:
    """Classify unmatched observed transport shape for candidate staging."""
    if observed_entry.transport == "stdio":
        return "unknown_stdio"
    if observed_entry.url and is_local_http_url(observed_entry.url):
        return "unknown_local_http"
    if observed_entry.url:
        return "unknown_remote_http"
    return None


def backend_shape(observed_entry: ObservedEntry) -> dict[str, Any]:
    """Return redacted backend shape for candidate review."""
    if observed_entry.transport == "stdio":
        return {
            "type": "stdio",
            "command": observed_entry.command,
            "args": observed_entry.args,
            "cwd": observed_entry.cwd,
            "env_names": observed_entry.env_names,
        }
    return {
        "type": observed_entry.transport,
        "url": observed_entry.url,
        "headers_present": observed_entry.headers_present,
    }


def candidate_reasons(observed_entry: ObservedEntry, classification: str) -> list[str]:
    """Return operator-visible staging reasons."""
    reasons = ["not_in_catalog"]
    if classification == "unknown_stdio":
        reasons.append("unknown_package")
    elif classification == "unknown_local_http":
        reasons.append("unsafe_local_http_endpoint")
    else:
        reasons.append("unknown_remote_http_endpoint")
    if observed_entry.enabled:
        reasons.append("active_direct_bypass")
    else:
        reasons.append("disabled_observed_entry")
    return reasons


def candidate_id(observed_entry: ObservedEntry) -> str:
    """Return deterministic candidate id for an observed entry."""
    digest = hashlib.sha256(
        json.dumps(backend_shape(observed_entry), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"cand_{digest[:24]}"


def is_local_http_url(value: str) -> bool:
    """Return whether a URL points at a local HTTP endpoint."""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme.lower() in {"http", "https"} and host in {"127.0.0.1", "localhost", "::1"}


def _candidate_exists(connection: sqlite3.Connection, candidate_id_value: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM catalog_candidates WHERE candidate_id = ?",
        (candidate_id_value,),
    ).fetchone()
    return row is not None


def _candidate_row(candidate: CatalogCandidate) -> tuple[Any, ...]:
    return (
        candidate.candidate_id,
        candidate.schema_version,
        candidate.source,
        candidate.observed_entry_id,
        candidate.proposed_name,
        candidate.classification,
        candidate.review_state,
        candidate.risk_tier,
        candidate.confidence,
        _canonical_json(candidate.backend_shape),
        int(candidate.approval_required),
        _canonical_json(candidate.reasons),
    )


def _candidate_from_row(row: sqlite3.Row) -> CatalogCandidate:
    return CatalogCandidate.from_dict(
        {
            "schema_version": int(row["schema_version"]),
            "candidate_id": str(row["candidate_id"]),
            "source": str(row["source"]),
            "observed_entry_id": str(row["observed_entry_id"]),
            "proposed_name": str(row["proposed_name"]),
            "classification": str(row["classification"]),
            "review_state": str(row["review_state"]),
            "risk_tier": str(row["risk_tier"]),
            "confidence": str(row["confidence"]),
            "backend_shape": json.loads(str(row["backend_shape_json"])),
            "approval_required": bool(row["approval_required"]),
            "reasons": json.loads(str(row["reasons_json"])),
        }
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
