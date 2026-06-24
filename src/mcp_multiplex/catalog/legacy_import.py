"""Legacy MCP Hub catalog import and normalization."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp_multiplex.catalog import CatalogStore
from mcp_multiplex.observability import EventStore
from mcp_multiplex.schemas import CatalogEntry, ValidationError
from mcp_multiplex.storage import migrate

UNSAFE_LEGACY_SHELL_CHARS = re.compile(r"[;&|`$<>(){}\\\n\r]")


class LegacyCatalogImportError(ValueError):
    """Raised when a legacy catalog export cannot be imported safely."""


@dataclass(frozen=True)
class LegacyCatalogImportPlan:
    """Normalized import plan for one legacy catalog export."""

    source: str
    catalog_path: str
    source_hash: str
    entries: list[CatalogEntry]
    errors: list[dict[str, str]]
    warnings: list[dict[str, str]]
    applied: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "catalog_path": self.catalog_path,
            "source_hash": self.source_hash,
            "entry_count": len(self.entries),
            "entries": [entry.to_dict() for entry in self.entries],
            "errors": self.errors,
            "warnings": self.warnings,
            "applied": self.applied,
        }


def plan_legacy_mcp_hub_catalog_import(catalog_path: Path) -> LegacyCatalogImportPlan:
    """Parse and normalize a legacy MCP Hub catalog export without mutation."""
    source_path = catalog_path.expanduser().resolve()
    if not source_path.is_file():
        raise LegacyCatalogImportError(f"legacy catalog not found: {source_path}")
    content = source_path.read_bytes()
    try:
        payload = json.loads(content.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise LegacyCatalogImportError(f"invalid legacy catalog JSON: {error}") from error
    raw_entries = _legacy_entries(payload)
    entries: list[CatalogEntry] = []
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for index, raw_entry in enumerate(raw_entries):
        entry_ref = _entry_ref(raw_entry, index)
        try:
            normalized, entry_warnings = _normalize_legacy_entry(raw_entry, source_path)
        except (LegacyCatalogImportError, ValidationError) as error:
            errors.append({"entry": entry_ref, "detail": str(error)})
            continue
        entries.append(normalized)
        warnings.extend({"entry": entry_ref, **warning} for warning in entry_warnings)
    return LegacyCatalogImportPlan(
        source="mcp-hub",
        catalog_path=str(source_path),
        source_hash=_sha256_bytes(content),
        entries=entries,
        errors=errors,
        warnings=warnings,
    )


def apply_legacy_mcp_hub_catalog_import(
    connection: sqlite3.Connection,
    catalog_path: Path,
    *,
    actor: str = "local_operator",
) -> LegacyCatalogImportPlan:
    """Import normalized legacy MCP Hub catalog entries into Multiplex state."""
    migrate(connection)
    plan = plan_legacy_mcp_hub_catalog_import(catalog_path)
    if not plan.ok:
        raise LegacyCatalogImportError("legacy catalog import has validation errors")
    store = CatalogStore(connection)
    for entry in plan.entries:
        store.upsert(entry)
    EventStore(connection).append(
        event_id=_event_id(plan.catalog_path, plan.source_hash),
        event_type="catalog.legacy_import",
        actor=actor,
        result="success",
        target_path=plan.catalog_path,
        before_hash=plan.source_hash,
        after_hash=plan.source_hash,
        payload={
            "source": plan.source,
            "catalog_path": plan.catalog_path,
            "source_hash": plan.source_hash,
            "entry_count": len(plan.entries),
            "catalog_ids": [entry.catalog_id for entry in plan.entries],
            "warnings": plan.warnings,
        },
    )
    return LegacyCatalogImportPlan(
        source=plan.source,
        catalog_path=plan.catalog_path,
        source_hash=plan.source_hash,
        entries=plan.entries,
        errors=plan.errors,
        warnings=plan.warnings,
        applied=True,
    )


def _legacy_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("servers"), list):
            entries = payload["servers"]
        elif isinstance(payload.get("catalog"), list):
            entries = payload["catalog"]
        elif isinstance(payload.get("servers"), dict):
            entries = [
                {"name": name, **entry} if isinstance(entry, dict) else {"name": name}
                for name, entry in payload["servers"].items()
            ]
        else:
            raise LegacyCatalogImportError(
                "legacy catalog JSON must be a list or contain servers/catalog entries"
            )
    else:
        raise LegacyCatalogImportError("legacy catalog JSON must be an object or list")
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise LegacyCatalogImportError("legacy catalog entries must be objects")
        result.append(dict(entry))
    return result


def _normalize_legacy_entry(
    raw_entry: dict[str, Any],
    source_path: Path,
) -> tuple[CatalogEntry, list[dict[str, str]]]:
    name = _server_name(raw_entry)
    backend, backend_warnings = _backend(raw_entry)
    env_names, warnings = _env_names(raw_entry)
    warnings = [*backend_warnings, *warnings]
    payload = {
        "schema_version": 1,
        "catalog_id": _catalog_id(raw_entry, name),
        "name": name,
        "canonical_name": _string(raw_entry, "canonical_name")
        or _string(raw_entry, "canonicalName")
        or f"legacy.{name}",
        "family_id": _string(raw_entry, "family_id") or _string(raw_entry, "familyId") or name,
        "variant_name": _string(raw_entry, "variant_name") or _string(raw_entry, "variantName"),
        "display_label": _string(raw_entry, "display_label")
        or _string(raw_entry, "displayLabel")
        or name,
        "aliases": _aliases(raw_entry, name),
        "review_state": _review_state(raw_entry),
        "lifecycle_state": _lifecycle_state(raw_entry),
        "risk_tier": _risk_tier(raw_entry),
        "provenance": [
            {
                "source": "legacy_mcp_hub",
                "source_ref": str(source_path),
                "observed_entry_id": None,
                "metadata": {
                    "legacy_name": name,
                    "import_review": "pending_operator_review",
                },
            }
        ],
        "transport": {
            "frontend": "streamable_http",
            "hub_path": f"/servers/{name}/mcp",
            "backend": {**backend, "env": env_names},
        },
        "runtime": _runtime(raw_entry, backend["type"]),
        "credentials": _credentials(raw_entry),
        "active_set": {
            "eligible_profiles": _string_list(raw_entry.get("eligible_profiles"))
            or _string_list(raw_entry.get("eligibleProfiles"))
            or ["coding-default"],
            "default_enabled": bool(raw_entry.get("default_enabled", False)),
        },
    }
    return CatalogEntry.from_dict(payload), warnings


def _server_name(raw_entry: dict[str, Any]) -> str:
    name = _string(raw_entry, "name") or _string(raw_entry, "id")
    if not name:
        raise LegacyCatalogImportError("legacy entry requires name")
    return name


def _catalog_id(raw_entry: dict[str, Any], name: str) -> str:
    value = _string(raw_entry, "catalog_id") or _string(raw_entry, "catalogId")
    if value:
        return value if value.startswith("srv_") else f"srv_{_slug(value)}"
    return f"srv_{_slug(name)}"


def _backend(raw_entry: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    legacy_backend = raw_entry.get("backend")
    backend = dict(legacy_backend) if isinstance(legacy_backend, dict) else raw_entry
    warnings: list[dict[str, str]] = []
    backend_type = _string(backend, "type") or _string(backend, "transport") or "stdio"
    if backend_type in {"streamable-http", "streamableHttp"}:
        backend_type = "streamable_http"
    if backend_type in {"remote-http", "remoteHttp"}:
        backend_type = "streamable_http"
    if backend_type == "stdio" and backend.get("port") is not None:
        backend_type = "streamable_http"
    if backend_type in {"streamable_http", "http", "sse"}:
        url = _string(backend, "url") or _local_port_url(backend)
        if not url:
            raise LegacyCatalogImportError("HTTP legacy entry requires url")
        if _string(backend, "command"):
            warnings.append(
                {
                    "code": "legacy_http_start_command_not_imported",
                    "detail": (
                        "legacy HTTP start command was not imported; backend URL "
                        "must already be reachable or be supervised separately"
                    ),
                }
            )
        return (
            {
                "type": backend_type,
                "command": None,
                "args": [],
                "cwd_policy": "none",
                "url": url,
            },
            warnings,
        )
    if backend_type == "stdio":
        command = _string(backend, "command")
        if not command:
            raise LegacyCatalogImportError("stdio legacy entry requires command")
        normalized_command, command_args, command_warnings = _split_legacy_stdio_command(command)
        return (
            {
                "type": "stdio",
                "command": normalized_command,
                "args": [*command_args, *_string_list(backend.get("args"))],
                "cwd_policy": _string(backend, "cwd_policy") or "none",
                "url": None,
            },
            [*warnings, *command_warnings],
        )
    raise LegacyCatalogImportError(f"type has unsupported value: {backend_type}")


def _env_names(raw_entry: dict[str, Any]) -> tuple[list[str], list[dict[str, str]]]:
    value = raw_entry.get("env")
    required = set(
        _string_list(raw_entry.get("required_env")) or _string_list(raw_entry.get("requiredEnv"))
    )
    warnings: list[dict[str, str]] = []
    if value is None:
        return sorted(required), warnings
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sorted(set(value) | required), warnings
    if isinstance(value, dict):
        warnings.append(
            {
                "code": "legacy_env_values_redacted",
                "detail": "legacy env object was converted to env names only",
            }
        )
        return sorted({str(key) for key in value} | required), warnings
    raise LegacyCatalogImportError("legacy env must be a list of names or object of values")


def _split_legacy_stdio_command(command: str) -> tuple[str, list[str], list[dict[str, str]]]:
    if not any(character.isspace() for character in command):
        return command, [], []
    if UNSAFE_LEGACY_SHELL_CHARS.search(command):
        raise LegacyCatalogImportError(
            "legacy stdio command shell string contains unsupported shell syntax"
        )
    try:
        parts = shlex.split(command)
    except ValueError as error:
        raise LegacyCatalogImportError(f"legacy stdio command cannot be parsed: {error}") from error
    if not parts:
        raise LegacyCatalogImportError("stdio legacy entry requires command")
    return (
        parts[0],
        parts[1:],
        [
            {
                "code": "legacy_stdio_command_split",
                "detail": "legacy stdio command string was split into command and args",
            }
        ],
    )


def _local_port_url(raw_entry: dict[str, Any]) -> str | None:
    port = raw_entry.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or port <= 0 or port > 65535:
        return None
    return f"http://127.0.0.1:{port}/mcp"


def _runtime(raw_entry: dict[str, Any], backend_type: str) -> dict[str, Any]:
    shareability = _string(raw_entry, "shareability")
    if not shareability:
        shareability = "isolated_per_frontend_session" if backend_type != "stdio" else "per_agent"
    if shareability == "global" and backend_type != "stdio":
        shareability = "per_workspace"
    return {
        "shareability": shareability,
        "concurrency": _string(raw_entry, "concurrency") or "serialized",
        "idle_timeout_sec": _positive_int(raw_entry.get("idle_timeout_sec"), default=600),
        "health_check": _string(raw_entry, "health_check") or "tools_list",
    }


def _credentials(raw_entry: dict[str, Any]) -> list[dict[str, Any]]:
    value = raw_entry.get("credentials", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise LegacyCatalogImportError("legacy credentials must be a list")
    result = []
    for credential in value:
        if not isinstance(credential, dict):
            raise LegacyCatalogImportError("legacy credential entries must be objects")
        normalized = dict(credential)
        if "value" in normalized:
            raise LegacyCatalogImportError("legacy credential contains raw value")
        result.append(normalized)
    return result


def _aliases(raw_entry: dict[str, Any], name: str) -> list[str]:
    aliases = set(_string_list(raw_entry.get("aliases")))
    package = _string(raw_entry, "package") or _string(raw_entry, "npm_package")
    if package:
        aliases.add(package)
    aliases.add(name)
    return sorted(aliases)


def _review_state(raw_entry: dict[str, Any]) -> str:
    state = _string(raw_entry, "review_state") or _string(raw_entry, "reviewState")
    return state if state in {"approved", "pending", "rejected", "quarantined"} else "pending"


def _lifecycle_state(raw_entry: dict[str, Any]) -> str:
    state = _string(raw_entry, "lifecycle_state") or _string(raw_entry, "lifecycleState")
    return state if state in {"enabled", "disabled", "deprecated"} else "enabled"


def _risk_tier(raw_entry: dict[str, Any]) -> str:
    tier = _string(raw_entry, "risk_tier") or _string(raw_entry, "riskTier")
    return tier if tier in {"low", "normal", "high"} else "normal"


def _string(raw_entry: dict[str, Any], key: str) -> str | None:
    value = raw_entry.get(key)
    return value if isinstance(value, str) and value else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _positive_int(value: Any, *, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _entry_ref(raw_entry: dict[str, Any], index: int) -> str:
    return _string(raw_entry, "name") or _string(raw_entry, "id") or f"entry[{index}]"


def _slug(value: str) -> str:
    slug = "".join(character.lower() if character.isalnum() else "_" for character in value)
    return "_".join(part for part in slug.split("_") if part) or "legacy"


def _sha256_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _event_id(catalog_path: str, source_hash: str) -> str:
    digest = hashlib.sha256(f"{catalog_path}\0{source_hash}".encode()).hexdigest()[:24]
    return f"evt_{digest}"
