"""Catalog identity matching and confidence scoring."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mcp_multiplex.catalog import CatalogStore, validate_routable_catalog_entry
from mcp_multiplex.schemas import CatalogEntry, ObservedEntry

MatchConfidence = Literal["none", "weak", "medium", "high"]
MatchReason = Literal[
    "exact_hub_url",
    "exact_backend_fingerprint",
    "normalized_backend_url",
    "alias_name",
    "mount_name",
    "not_routable",
]


@dataclass(frozen=True)
class CatalogMatch:
    """Catalog match result for one observed entry."""

    observed_entry_id: str
    catalog_id: str | None
    confidence: MatchConfidence
    reasons: list[MatchReason] = field(default_factory=list)
    auto_apply_allowed: bool = False
    routable: bool = False
    routability_reasons: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.catalog_id is not None and self.confidence != "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_entry_id": self.observed_entry_id,
            "catalog_id": self.catalog_id,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "auto_apply_allowed": self.auto_apply_allowed,
            "routable": self.routable,
            "routability_reasons": self.routability_reasons,
        }


def match_observed_entry(
    observed_entry: ObservedEntry,
    catalog_entries: list[CatalogEntry],
) -> CatalogMatch:
    """Match one observed entry against catalog entries."""
    candidates = [_score_entry(observed_entry, entry) for entry in catalog_entries]
    matches = [candidate for candidate in candidates if candidate.matched]
    if not matches:
        return CatalogMatch(
            observed_entry_id=observed_entry.observed_entry_id,
            catalog_id=None,
            confidence="none",
        )
    return sorted(
        matches,
        key=lambda item: (
            _confidence_rank(item.confidence),
            item.auto_apply_allowed,
            item.catalog_id or "",
        ),
        reverse=True,
    )[0]


def match_observed_entry_from_store(
    observed_entry: ObservedEntry,
    store: CatalogStore,
) -> CatalogMatch:
    """Match one observed entry against all stored catalog entries."""
    return match_observed_entry(observed_entry, store.list())


def backend_fingerprint_for_observed_entry(observed_entry: ObservedEntry) -> str | None:
    """Return a stable stdio backend fingerprint for direct observed entries."""
    if observed_entry.transport != "stdio":
        return None
    payload = {
        "type": "stdio",
        "command": observed_entry.command,
        "args": observed_entry.args,
    }
    return _fingerprint(payload)


def backend_fingerprint_for_catalog_entry(catalog_entry: CatalogEntry) -> str:
    """Return a stable stdio backend fingerprint for a catalog entry."""
    backend = catalog_entry.transport.backend
    payload = {
        "type": backend.type,
        "command": backend.command,
        "args": backend.args,
    }
    return _fingerprint(payload)


def normalize_url(value: str) -> str:
    """Normalize URLs for backend identity comparison."""
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{hostname}{port}"
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, path, query, ""))


def _score_entry(observed_entry: ObservedEntry, catalog_entry: CatalogEntry) -> CatalogMatch:
    routability = validate_routable_catalog_entry(catalog_entry)
    reasons: list[MatchReason] = []
    if is_exact_hub_url_match(observed_entry, catalog_entry):
        reasons.append("exact_hub_url")
    if is_exact_backend_fingerprint_match(observed_entry, catalog_entry):
        reasons.append("exact_backend_fingerprint")
    if is_normalized_backend_url_match(observed_entry, catalog_entry):
        reasons.append("normalized_backend_url")
    if is_alias_name_match(observed_entry, catalog_entry):
        reasons.append("alias_name")
    if observed_entry.mount_name == catalog_entry.name:
        reasons.append("mount_name")

    confidence = _confidence_for_reasons(reasons)
    if not reasons:
        return CatalogMatch(
            observed_entry_id=observed_entry.observed_entry_id,
            catalog_id=None,
            confidence="none",
        )

    result_reasons = list(dict.fromkeys(reasons))
    if not routability.routable:
        result_reasons.append("not_routable")
    auto_apply_allowed = (
        confidence == "high"
        and routability.routable
        and observed_entry.enabled
        and observed_entry.parser_confidence == "complete"
    )
    return CatalogMatch(
        observed_entry_id=observed_entry.observed_entry_id,
        catalog_id=catalog_entry.catalog_id,
        confidence=confidence,
        reasons=result_reasons,
        auto_apply_allowed=auto_apply_allowed,
        routable=routability.routable,
        routability_reasons=routability.reasons,
    )


def is_exact_hub_url_match(observed_entry: ObservedEntry, catalog_entry: CatalogEntry) -> bool:
    if not observed_entry.url:
        return False
    expected = f"http://127.0.0.1:30000{catalog_entry.transport.hub_path}"
    return observed_entry.transport == "streamable_http" and normalize_url(
        observed_entry.url
    ) == normalize_url(expected)


def is_exact_backend_fingerprint_match(
    observed_entry: ObservedEntry,
    catalog_entry: CatalogEntry,
) -> bool:
    if catalog_entry.transport.backend.type != "stdio":
        return False
    observed = backend_fingerprint_for_observed_entry(observed_entry)
    if observed is None:
        return False
    return observed == backend_fingerprint_for_catalog_entry(catalog_entry)


def is_normalized_backend_url_match(
    observed_entry: ObservedEntry,
    catalog_entry: CatalogEntry,
) -> bool:
    backend = catalog_entry.transport.backend
    if observed_entry.transport == "stdio" or not observed_entry.url or not backend.url:
        return False
    return normalize_url(observed_entry.url) == normalize_url(backend.url)


def is_alias_name_match(observed_entry: ObservedEntry, catalog_entry: CatalogEntry) -> bool:
    names = set(catalog_entry.aliases)
    normalized_names = {_normalize_name(value) for value in names if value}
    observed_names = {
        _normalize_name(observed_entry.mount_name),
        *(_normalize_name(arg) for arg in observed_entry.args),
    }
    return bool(normalized_names & observed_names)


def _confidence_for_reasons(reasons: list[MatchReason]) -> MatchConfidence:
    if "exact_hub_url" in reasons or "exact_backend_fingerprint" in reasons:
        return "high"
    if "normalized_backend_url" in reasons:
        return "medium"
    if "alias_name" in reasons or "mount_name" in reasons:
        return "weak"
    return "none"


def _confidence_rank(confidence: MatchConfidence) -> int:
    return {"none": 0, "weak": 1, "medium": 2, "high": 3}[confidence]


def _normalize_name(value: str) -> str:
    return value.strip().lower()


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"sha256:{digest}"
