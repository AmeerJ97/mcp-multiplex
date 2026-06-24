from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from mcp_multiplex.catalog import (
    CatalogStore,
    CatalogStoreError,
    validate_routable_catalog_entry,
)
from mcp_multiplex.schemas import CatalogEntry, ValidationError
from mcp_multiplex.storage import connect
from tests.test_schema_models import catalog_entry_payload


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "multiplex.db")


def catalog_entry(**updates: object) -> CatalogEntry:
    payload = catalog_entry_payload()
    payload.update(updates)
    return CatalogEntry.from_dict(payload)


def test_catalog_store_persists_entry_aliases_and_provenance(
    connection: sqlite3.Connection,
) -> None:
    payload = catalog_entry_payload()
    payload["provenance"] = [
        {
            "source": "seed_fixture",
            "source_ref": "docs/SCHEMAS_AND_CONTRACTS.md",
            "observed_entry_id": None,
            "metadata": {"imported_by": "test"},
        }
    ]
    payload["aliases"] = ["@upstash/context7-mcp", "context7-mcp"]
    store = CatalogStore(connection)

    stored = store.upsert(CatalogEntry.from_dict(payload))

    assert stored.to_dict() == payload
    assert store.show("srv_context7").to_dict() == payload
    rows = connection.execute(
        """
        SELECT alias
        FROM catalog_aliases
        WHERE catalog_id = 'srv_context7'
        ORDER BY alias
        """
    ).fetchall()
    assert [str(row["alias"]) for row in rows] == ["@upstash/context7-mcp", "context7-mcp"]
    provenance = connection.execute(
        """
        SELECT source, source_ref, metadata_json
        FROM catalog_provenance
        WHERE catalog_id = 'srv_context7'
        """
    ).fetchone()
    assert provenance["source"] == "seed_fixture"
    assert json.loads(str(provenance["metadata_json"])) == {"imported_by": "test"}


def test_catalog_store_lists_entries_deterministically(connection: sqlite3.Connection) -> None:
    store = CatalogStore(connection)
    second_payload = catalog_entry_payload()
    second_payload.update(
        {
            "catalog_id": "srv_github",
            "name": "github",
            "canonical_name": "modelcontextprotocol.github",
            "family_id": "github",
            "variant_name": "official_npm",
            "display_label": "GitHub",
            "aliases": ["github-mcp"],
            "transport": {
                "frontend": "streamable_http",
                "hub_path": "/servers/github/mcp",
                "backend": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "cwd_policy": "none",
                    "env": ["GITHUB_TOKEN"],
                    "url": None,
                },
            },
        }
    )

    store.upsert(CatalogEntry.from_dict(second_payload))
    store.upsert(catalog_entry())

    assert [entry.catalog_id for entry in store.list()] == ["srv_github", "srv_context7"]


def test_required_metadata_validation_allows_complete_approved_entry(
    connection: sqlite3.Connection,
) -> None:
    store = CatalogStore(connection)
    stored = store.upsert(catalog_entry())

    check = validate_routable_catalog_entry(stored)

    assert check.routable is True
    assert check.reasons == []
    assert store.require_routable("srv_context7") == stored


def test_required_metadata_validation_rejects_unapproved_or_disabled_entries(
    connection: sqlite3.Connection,
) -> None:
    pending = catalog_entry(review_state="pending")
    disabled = catalog_entry(catalog_id="srv_disabled", lifecycle_state="disabled")

    pending_check = validate_routable_catalog_entry(pending)
    disabled_check = validate_routable_catalog_entry(disabled)

    assert pending_check.routable is False
    assert pending_check.reasons == ["review_state must be approved"]
    assert disabled_check.routable is False
    assert disabled_check.reasons == ["lifecycle_state must be enabled"]


def test_required_metadata_validation_rejects_wrong_hub_path(
    connection: sqlite3.Connection,
) -> None:
    payload = catalog_entry_payload()
    transport = dict(cast(dict[str, Any], payload["transport"]))
    transport["hub_path"] = "/servers/wrong/mcp"
    payload["transport"] = transport
    entry = CatalogEntry.from_dict(payload)

    check = validate_routable_catalog_entry(entry)

    assert check.routable is False
    assert check.reasons == ["transport.hub_path must preserve the catalog server name"]


def test_require_routable_raises_for_incomplete_metadata(
    connection: sqlite3.Connection,
) -> None:
    store = CatalogStore(connection)
    stored = store.upsert(catalog_entry(review_state="pending"))

    with pytest.raises(CatalogStoreError, match="review_state must be approved"):
        store.require_routable(stored.catalog_id)


def test_catalog_store_updates_review_and_lifecycle_state(
    connection: sqlite3.Connection,
) -> None:
    store = CatalogStore(connection)
    store.upsert(catalog_entry(review_state="pending", lifecycle_state="disabled"))

    before, after = store.set_review_state(
        "srv_context7",
        review_state="approved",
        lifecycle_state="enabled",
    )

    assert before.review_state == "pending"
    assert before.lifecycle_state == "disabled"
    assert after.review_state == "approved"
    assert after.lifecycle_state == "enabled"
    assert store.require_routable("srv_context7") == after


def test_schema_validation_rejects_missing_backend_metadata() -> None:
    payload = catalog_entry_payload()
    transport = dict(cast(dict[str, Any], payload["transport"]))
    backend = dict(cast(dict[str, Any], transport["backend"]))
    backend["command"] = None
    transport["backend"] = backend
    payload["transport"] = transport

    with pytest.raises(ValidationError, match="stdio backend transport requires command"):
        CatalogEntry.from_dict(payload)


def test_catalog_provenance_requires_source(connection: sqlite3.Connection) -> None:
    payload = catalog_entry_payload()
    payload["provenance"] = [{"metadata": {"source": "missing"}}]

    with pytest.raises(CatalogStoreError, match="provenance requires source"):
        CatalogStore(connection).upsert(CatalogEntry.from_dict(payload))
