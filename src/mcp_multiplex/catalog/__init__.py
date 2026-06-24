"""Catalog storage and required metadata validation."""

from __future__ import annotations

import builtins
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from mcp_multiplex.schemas import CatalogEntry, ValidationError
from mcp_multiplex.storage import migrate


class CatalogStoreError(ValueError):
    """Raised for invalid catalog storage or routing-readiness input."""


@dataclass(frozen=True)
class RoutabilityCheck:
    """Result of catalog metadata validation for routing."""

    catalog_id: str
    routable: bool
    reasons: list[str]

    def require(self) -> None:
        """Raise when the catalog entry is not safe to route."""
        if not self.routable:
            raise CatalogStoreError(
                f"catalog entry {self.catalog_id} is not routable: {', '.join(self.reasons)}"
            )


class CatalogStore:
    """SQLite-backed catalog repository."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate(connection)

    def upsert(self, entry: CatalogEntry) -> CatalogEntry:
        """Insert or update one catalog entry, including aliases and provenance."""
        validated = CatalogEntry.from_dict(entry.to_dict())
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO catalog_entries (
                  catalog_id,
                  schema_version,
                  name,
                  canonical_name,
                  family_id,
                  variant_name,
                  display_label,
                  review_state,
                  lifecycle_state,
                  risk_tier,
                  transport_json,
                  runtime_json,
                  credentials_json,
                  active_set_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(catalog_id) DO UPDATE SET
                  schema_version = excluded.schema_version,
                  name = excluded.name,
                  canonical_name = excluded.canonical_name,
                  family_id = excluded.family_id,
                  variant_name = excluded.variant_name,
                  display_label = excluded.display_label,
                  review_state = excluded.review_state,
                  lifecycle_state = excluded.lifecycle_state,
                  risk_tier = excluded.risk_tier,
                  transport_json = excluded.transport_json,
                  runtime_json = excluded.runtime_json,
                  credentials_json = excluded.credentials_json,
                  active_set_json = excluded.active_set_json,
                  updated_at = CURRENT_TIMESTAMP
                """,
                _catalog_entry_row(validated),
            )
            self.connection.execute(
                "DELETE FROM catalog_aliases WHERE catalog_id = ?", (validated.catalog_id,)
            )
            for alias in sorted(set(validated.aliases)):
                self.connection.execute(
                    """
                    INSERT INTO catalog_aliases (alias_id, catalog_id, alias, alias_kind)
                    VALUES (?, ?, ?, 'name')
                    """,
                    (_alias_id(validated.catalog_id, alias), validated.catalog_id, alias),
                )
            self.connection.execute(
                "DELETE FROM catalog_provenance WHERE catalog_id = ?", (validated.catalog_id,)
            )
            for index, provenance in enumerate(validated.provenance):
                self.connection.execute(
                    """
                    INSERT INTO catalog_provenance (
                      provenance_id,
                      catalog_id,
                      source,
                      source_ref,
                      observed_entry_id,
                      metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    _provenance_row(validated.catalog_id, index, provenance),
                )
        return self.show(validated.catalog_id)

    def show(self, catalog_id: str) -> CatalogEntry:
        """Return one catalog entry by id."""
        row = self.connection.execute(
            """
            SELECT *
            FROM catalog_entries
            WHERE catalog_id = ?
            """,
            (catalog_id,),
        ).fetchone()
        if row is None:
            raise KeyError(catalog_id)
        return _catalog_entry_from_row(
            row,
            aliases=self._aliases(catalog_id),
            provenance=self._provenance(catalog_id),
        )

    def list(self) -> list[CatalogEntry]:
        """List catalog entries in deterministic identity order."""
        rows = self.connection.execute(
            """
            SELECT catalog_id
            FROM catalog_entries
            ORDER BY canonical_name, variant_name, catalog_id
            """
        ).fetchall()
        return [self.show(str(row["catalog_id"])) for row in rows]

    def validate_routable(self, catalog_id: str) -> RoutabilityCheck:
        """Validate required metadata before routing can use an entry."""
        return validate_routable_catalog_entry(self.show(catalog_id))

    def require_routable(self, catalog_id: str) -> CatalogEntry:
        """Return a catalog entry only if it is safe to route."""
        check = self.validate_routable(catalog_id)
        check.require()
        return self.show(catalog_id)

    def set_review_state(
        self,
        catalog_id: str,
        *,
        review_state: str,
        lifecycle_state: str | None = None,
    ) -> tuple[CatalogEntry, CatalogEntry]:
        """Update review/lifecycle state for one catalog entry."""
        before = self.show(catalog_id)
        after_payload = before.to_dict()
        after_payload["review_state"] = review_state
        if lifecycle_state is not None:
            after_payload["lifecycle_state"] = lifecycle_state
        after = CatalogEntry.from_dict(after_payload)
        with self.connection:
            self.connection.execute(
                """
                UPDATE catalog_entries
                SET
                  review_state = ?,
                  lifecycle_state = ?,
                  updated_at = CURRENT_TIMESTAMP
                WHERE catalog_id = ?
                """,
                (after.review_state, after.lifecycle_state, catalog_id),
            )
        return before, self.show(catalog_id)

    def _aliases(self, catalog_id: str) -> builtins.list[str]:
        rows = self.connection.execute(
            """
            SELECT alias
            FROM catalog_aliases
            WHERE catalog_id = ?
            ORDER BY alias
            """,
            (catalog_id,),
        ).fetchall()
        return [str(row["alias"]) for row in rows]

    def _provenance(self, catalog_id: str) -> builtins.list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT source, source_ref, observed_entry_id, metadata_json
            FROM catalog_provenance
            WHERE catalog_id = ?
            ORDER BY provenance_id
            """,
            (catalog_id,),
        ).fetchall()
        return [
            {
                "source": str(row["source"]),
                "source_ref": row["source_ref"],
                "observed_entry_id": row["observed_entry_id"],
                "metadata": json.loads(str(row["metadata_json"])),
            }
            for row in rows
        ]


def validate_routable_catalog_entry(entry: CatalogEntry) -> RoutabilityCheck:
    """Validate catalog metadata required before data-plane routing."""
    try:
        validated = CatalogEntry.from_dict(entry.to_dict())
    except ValidationError as error:
        return RoutabilityCheck(
            catalog_id=entry.catalog_id or "<invalid>",
            routable=False,
            reasons=[str(error)],
        )
    reasons: list[str] = []
    if validated.review_state != "approved":
        reasons.append("review_state must be approved")
    if validated.lifecycle_state != "enabled":
        reasons.append("lifecycle_state must be enabled")
    if not validated.transport.hub_path.startswith(f"/servers/{validated.name}/"):
        reasons.append("transport.hub_path must preserve the catalog server name")
    if validated.transport.frontend != "streamable_http":
        reasons.append("transport.frontend must be streamable_http")
    if not validated.runtime.shareability:
        reasons.append("runtime.shareability is required")
    if not validated.runtime.concurrency:
        reasons.append("runtime.concurrency is required")
    if not validated.runtime.health_check:
        reasons.append("runtime.health_check is required")
    if validated.transport.backend.type == "stdio" and not validated.transport.backend.command:
        reasons.append("stdio backend command is required")
    if validated.transport.backend.type != "stdio" and not validated.transport.backend.url:
        reasons.append("HTTP backend url is required")
    return RoutabilityCheck(
        catalog_id=validated.catalog_id,
        routable=not reasons,
        reasons=reasons,
    )


def _catalog_entry_row(entry: CatalogEntry) -> tuple[Any, ...]:
    return (
        entry.catalog_id,
        entry.schema_version,
        entry.name,
        entry.canonical_name,
        entry.family_id,
        entry.variant_name,
        entry.display_label,
        entry.review_state,
        entry.lifecycle_state,
        entry.risk_tier,
        _canonical_json(entry.transport.to_dict()),
        _canonical_json(entry.runtime.to_dict()),
        _canonical_json(entry.credentials),
        _canonical_json(entry.active_set.to_dict()),
    )


def _catalog_entry_from_row(
    row: sqlite3.Row,
    *,
    aliases: list[str],
    provenance: list[dict[str, Any]],
) -> CatalogEntry:
    return CatalogEntry.from_dict(
        {
            "schema_version": int(row["schema_version"]),
            "catalog_id": str(row["catalog_id"]),
            "name": str(row["name"]),
            "canonical_name": str(row["canonical_name"]),
            "family_id": str(row["family_id"]),
            "variant_name": row["variant_name"],
            "display_label": str(row["display_label"]),
            "aliases": aliases,
            "review_state": str(row["review_state"]),
            "lifecycle_state": str(row["lifecycle_state"]),
            "risk_tier": str(row["risk_tier"]),
            "provenance": provenance,
            "transport": json.loads(str(row["transport_json"])),
            "runtime": json.loads(str(row["runtime_json"])),
            "credentials": json.loads(str(row["credentials_json"])),
            "active_set": json.loads(str(row["active_set_json"])),
        }
    )


def _provenance_row(
    catalog_id: str,
    index: int,
    provenance: dict[str, Any],
) -> tuple[str, str, str, str | None, str | None, str]:
    source = provenance.get("source")
    if not isinstance(source, str) or not source:
        raise CatalogStoreError("catalog provenance requires source")
    source_ref = provenance.get("source_ref")
    observed_entry_id = provenance.get("observed_entry_id")
    metadata = provenance.get("metadata", {})
    if source_ref is not None and not isinstance(source_ref, str):
        raise CatalogStoreError("catalog provenance source_ref must be a string or null")
    if observed_entry_id is not None and not isinstance(observed_entry_id, str):
        raise CatalogStoreError("catalog provenance observed_entry_id must be a string or null")
    if not isinstance(metadata, dict):
        raise CatalogStoreError("catalog provenance metadata must be an object")
    return (
        f"{catalog_id}:prov:{index}",
        catalog_id,
        source,
        source_ref,
        observed_entry_id,
        _canonical_json(metadata),
    )


def _alias_id(catalog_id: str, alias: str) -> str:
    digest = hashlib.sha256(f"{catalog_id}\0{alias}".encode()).hexdigest()
    return f"{catalog_id}:alias:{digest[:16]}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


from mcp_multiplex.catalog.candidates import (  # noqa: E402
    CandidateStageResult,
    CatalogCandidateStore,
    backend_shape,
    candidate_classification,
    candidate_from_observed_entry,
    candidate_id,
    candidate_reasons,
    is_local_http_url,
    stage_unknown_candidate,
)
from mcp_multiplex.catalog.legacy_import import (  # noqa: E402
    LegacyCatalogImportError,
    LegacyCatalogImportPlan,
    apply_legacy_mcp_hub_catalog_import,
    plan_legacy_mcp_hub_catalog_import,
)
from mcp_multiplex.catalog.matching import (  # noqa: E402
    CatalogMatch,
    backend_fingerprint_for_catalog_entry,
    backend_fingerprint_for_observed_entry,
    match_observed_entry,
    match_observed_entry_from_store,
    normalize_url,
)

__all__ = [
    "CandidateStageResult",
    "CatalogMatch",
    "CatalogCandidateStore",
    "CatalogStore",
    "CatalogStoreError",
    "LegacyCatalogImportError",
    "LegacyCatalogImportPlan",
    "RoutabilityCheck",
    "apply_legacy_mcp_hub_catalog_import",
    "backend_fingerprint_for_catalog_entry",
    "backend_fingerprint_for_observed_entry",
    "backend_shape",
    "candidate_classification",
    "candidate_from_observed_entry",
    "candidate_id",
    "candidate_reasons",
    "is_local_http_url",
    "match_observed_entry",
    "match_observed_entry_from_store",
    "normalize_url",
    "plan_legacy_mcp_hub_catalog_import",
    "stage_unknown_candidate",
    "validate_routable_catalog_entry",
]
