from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from mcp_multiplex.adapters import parse_codex_config
from mcp_multiplex.catalog import (
    CatalogStore,
    backend_fingerprint_for_catalog_entry,
    backend_fingerprint_for_observed_entry,
    match_observed_entry,
    match_observed_entry_from_store,
    normalize_url,
)
from mcp_multiplex.schemas import CatalogEntry, ObservedEntry
from mcp_multiplex.storage import connect
from tests.test_schema_models import catalog_entry_payload, observed_entry_payload

FIXTURE_DIR = Path("tests/fixtures/agents/codex")


def catalog_entry(**updates: object) -> CatalogEntry:
    payload = catalog_entry_payload()
    payload.update(updates)
    return CatalogEntry.from_dict(payload)


def direct_context7() -> ObservedEntry:
    return parse_codex_config(FIXTURE_DIR / "direct-context7.input.toml").observed_entries[0]


def hub_context7() -> ObservedEntry:
    return parse_codex_config(FIXTURE_DIR / "hub-routed.input.toml").observed_entries[0]


def observed_http(url: str, *, mount_name: str = "remote_docs") -> ObservedEntry:
    payload = observed_entry_payload()
    payload.update(
        {
            "observed_entry_id": f"obs_{mount_name}",
            "mount_name": mount_name,
            "transport": "streamable_http",
            "command": None,
            "args": [],
            "url": url,
            "entry_hash": "sha256:http",
        }
    )
    return ObservedEntry.from_dict(payload)


def http_catalog_entry(url: str) -> CatalogEntry:
    payload = catalog_entry_payload()
    transport = dict(cast(dict[str, Any], payload["transport"]))
    transport["hub_path"] = "/servers/remote_docs/mcp"
    transport["backend"] = {
        "type": "streamable_http",
        "command": None,
        "args": [],
        "cwd_policy": "none",
        "env": [],
        "url": url,
    }
    runtime = dict(cast(dict[str, Any], payload["runtime"]))
    runtime["shareability"] = "per_workspace"
    payload.update(
        {
            "catalog_id": "srv_remote_docs",
            "name": "remote_docs",
            "canonical_name": "docs.remote",
            "family_id": "remote_docs",
            "variant_name": "hosted_http",
            "display_label": "Remote Docs",
            "aliases": ["remote-docs"],
            "transport": transport,
            "runtime": runtime,
        }
    )
    return CatalogEntry.from_dict(payload)


def test_backend_fingerprint_matches_direct_known_entry() -> None:
    entry = direct_context7()
    catalog = catalog_entry()

    assert backend_fingerprint_for_observed_entry(entry) == backend_fingerprint_for_catalog_entry(
        catalog
    )
    match = match_observed_entry(entry, [catalog])

    assert match.to_dict() == {
        "observed_entry_id": entry.observed_entry_id,
        "catalog_id": "srv_context7",
        "confidence": "high",
        "reasons": ["exact_backend_fingerprint", "alias_name", "mount_name"],
        "auto_apply_allowed": True,
        "routable": True,
        "routability_reasons": [],
    }


def test_exact_hub_url_matches_catalog_entry() -> None:
    entry = hub_context7()
    match = match_observed_entry(entry, [catalog_entry()])

    assert match.confidence == "high"
    assert match.reasons == ["exact_hub_url", "mount_name"]
    assert match.auto_apply_allowed is True


def test_normalized_backend_url_match_is_medium_confidence() -> None:
    catalog = http_catalog_entry("HTTP://Example.COM/mcp/?b=2&a=1")
    entry = observed_http("http://example.com/mcp?a=1&b=2")

    assert normalize_url("HTTP://Example.COM/mcp/?b=2&a=1") == "http://example.com/mcp?a=1&b=2"
    match = match_observed_entry(entry, [catalog])

    assert match.catalog_id == "srv_remote_docs"
    assert match.confidence == "medium"
    assert match.reasons == ["normalized_backend_url", "mount_name"]
    assert match.auto_apply_allowed is False
    assert match.routable is True


def test_alias_only_match_is_weak_and_cannot_auto_apply() -> None:
    payload = observed_entry_payload()
    payload.update(
        {
            "observed_entry_id": "obs_alias_only",
            "mount_name": "context7-mcp",
            "command": "uvx",
            "args": ["some-other-server"],
            "entry_hash": "sha256:aliasonly",
        }
    )
    entry = ObservedEntry.from_dict(payload)
    match = match_observed_entry(entry, [catalog_entry()])

    assert match.catalog_id == "srv_context7"
    assert match.confidence == "weak"
    assert match.reasons == ["alias_name"]
    assert match.auto_apply_allowed is False


def test_not_routable_entry_can_match_but_not_auto_apply() -> None:
    entry = direct_context7()
    catalog = catalog_entry(review_state="pending")

    match = match_observed_entry(entry, [catalog])

    assert match.catalog_id == "srv_context7"
    assert match.confidence == "high"
    assert "not_routable" in match.reasons
    assert match.auto_apply_allowed is False
    assert match.routability_reasons == ["review_state must be approved"]


def test_store_match_uses_persisted_catalog_entries(tmp_path: Path) -> None:
    connection = connect(tmp_path / "multiplex.db")
    store = CatalogStore(connection)
    store.upsert(catalog_entry())

    match = match_observed_entry_from_store(direct_context7(), store)

    assert match.catalog_id == "srv_context7"
    assert match.confidence == "high"
    assert match.auto_apply_allowed is True


def test_no_match_returns_none_confidence() -> None:
    payload = observed_entry_payload()
    payload.update(
        {
            "observed_entry_id": "obs_unknown",
            "mount_name": "unknown",
            "command": "uvx",
            "args": ["unknown-mcp"],
            "entry_hash": "sha256:unknown",
        }
    )
    match = match_observed_entry(ObservedEntry.from_dict(payload), [catalog_entry()])

    assert match.catalog_id is None
    assert match.confidence == "none"
    assert match.auto_apply_allowed is False
