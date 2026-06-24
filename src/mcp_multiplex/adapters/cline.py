"""Cline config parser and observed-entry normalization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_multiplex.adapters._headers import header_names_present
from mcp_multiplex.schemas import ObservedEntry
from mcp_multiplex.security import HUB_ORIGIN

CLINE_AGENT_KIND = "cline"
CLINE_RAW_SHAPE = "cline-json"
CLINE_MULTIPLEX_HELPER = "cline-mcp-multiplex-remote.sh"
MCP_HUB_URL = f"{HUB_ORIGIN}/servers/mcp_hub/mcp"
KNOWN_CLINE_ENTRY_KEYS = {
    "command",
    "args",
    "url",
    "transport",
    "env",
    "cwd",
    "enabled",
    "disabled",
    "enabled_tools",
    "disabled_tools",
    "approval_policy",
    "autoApprove",
    "alwaysAllow",
    "headers",
}


class ClineAdapterError(ValueError):
    """Raised when Cline config cannot be parsed or normalized."""


@dataclass(frozen=True)
class ParsedClineConfig:
    """Parsed Cline config with normalized observed entries."""

    config_path: str
    observed_entries: list[ObservedEntry]
    unsupported_fields: dict[str, dict[str, Any]] = field(default_factory=dict)

    def observed_dicts(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self.observed_entries]


def parse_cline_config(
    path: Path,
    *,
    agent_id: str = "agent_cline_user_default",
) -> ParsedClineConfig:
    """Parse a Cline JSON config without mutating or rewriting it."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ClineAdapterError(f"invalid Cline JSON at {path}: {error}") from error
    except OSError as error:
        message = f"cannot read Cline config at {path}: {error}"
        raise ClineAdapterError(message) from error

    if not isinstance(parsed, dict):
        raise ClineAdapterError("Cline config root must be an object")
    servers = parsed.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ClineAdapterError("Cline mcpServers must be an object")

    observed_entries: list[ObservedEntry] = []
    unsupported_fields: dict[str, dict[str, Any]] = {}
    for mount_name in sorted(servers):
        raw_entry = servers[mount_name]
        if not isinstance(raw_entry, dict):
            raise ClineAdapterError(f"Cline MCP entry {mount_name} must be an object")
        unsupported = {
            key: value for key, value in raw_entry.items() if key not in KNOWN_CLINE_ENTRY_KEYS
        }
        if unsupported:
            unsupported_fields[str(mount_name)] = unsupported
        observed_entries.append(
            normalize_cline_entry(
                mount_name=str(mount_name),
                raw_entry=raw_entry,
                config_path=str(path),
                agent_id=agent_id,
                parser_confidence="partial" if unsupported else "complete",
            )
        )

    return ParsedClineConfig(
        config_path=str(path),
        observed_entries=observed_entries,
        unsupported_fields=unsupported_fields,
    )


def normalize_cline_entry(
    *,
    mount_name: str,
    raw_entry: dict[str, Any],
    config_path: str,
    agent_id: str,
    parser_confidence: str,
) -> ObservedEntry:
    """Normalize one Cline MCP entry into the observed-entry schema."""
    command = _optional_string(raw_entry, "command")
    url = _optional_string(raw_entry, "url") or _transport_url(raw_entry)
    if mount_name == "mcp_hub" and _is_multiplex_helper_command(command):
        url = MCP_HUB_URL
    if url:
        transport = "streamable_http"
    elif command:
        transport = "stdio"
    else:
        raise ClineAdapterError(f"Cline MCP entry {mount_name} needs command or url")

    args = _string_list(raw_entry.get("args", []), field_name=f"{mount_name}.args")
    env = raw_entry.get("env", {})
    if env is None:
        env = {}
    if not isinstance(env, dict):
        raise ClineAdapterError(f"Cline MCP entry {mount_name}.env must be an object")
    env_names = sorted(str(key) for key in env)
    disabled_tools = _string_list(
        raw_entry.get("disabled_tools", []), field_name=f"{mount_name}.disabled_tools"
    )
    enabled_tools_raw = raw_entry.get("enabled_tools")
    enabled_tools = (
        None
        if enabled_tools_raw is None
        else _string_list(enabled_tools_raw, field_name=f"{mount_name}.enabled_tools")
    )
    enabled = bool(raw_entry.get("enabled", True)) and not bool(raw_entry.get("disabled", False))
    payload = {
        "schema_version": 1,
        "observed_entry_id": observed_entry_id(agent_id, config_path, mount_name),
        "agent_id": agent_id,
        "agent_kind": CLINE_AGENT_KIND,
        "config_path": config_path,
        "container_path": ["mcpServers", mount_name],
        "mount_name": mount_name,
        "enabled": enabled,
        "transport": transport,
        "command": command,
        "args": args,
        "url": url,
        "headers_present": _header_names_present(
            mount_name=mount_name,
            raw_entry=raw_entry,
            command=command,
        ),
        "env_names": env_names,
        "cwd": _optional_string(raw_entry, "cwd"),
        "tool_filters": {"enabled_tools": enabled_tools, "disabled_tools": disabled_tools},
        "approval_policy": _optional_string(raw_entry, "approval_policy"),
        "entry_hash": entry_hash(mount_name, raw_entry),
        "raw_shape": CLINE_RAW_SHAPE,
        "parser_confidence": parser_confidence,
    }
    return ObservedEntry.from_dict(payload)


def observed_entry_id(agent_id: str, config_path: str, mount_name: str) -> str:
    digest = hashlib.sha256(f"{agent_id}\0{config_path}\0{mount_name}".encode()).hexdigest()
    return f"obs_{digest[:24]}"


def entry_hash(mount_name: str, raw_entry: dict[str, Any]) -> str:
    payload = {"mount_name": mount_name, "raw_entry": _json_safe(raw_entry)}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"sha256:{digest}"


def _header_names_present(
    *,
    mount_name: str,
    raw_entry: dict[str, Any],
    command: str | None,
) -> list[str]:
    names = set(header_names_present(raw_entry, include_transport_headers=True))
    if mount_name == "mcp_hub" and _is_multiplex_helper_command(command):
        names.add("Authorization")
    return sorted(names)


def _is_multiplex_helper_command(command: str | None) -> bool:
    if command is None:
        return False
    return Path(command).name == CLINE_MULTIPLEX_HELPER


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _optional_string(raw_entry: dict[str, Any], key: str) -> str | None:
    value = raw_entry.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ClineAdapterError(f"{key} must be a string")
    return value


def _transport_url(raw_entry: dict[str, Any]) -> str | None:
    transport = raw_entry.get("transport")
    if transport is None:
        return None
    if not isinstance(transport, dict):
        raise ClineAdapterError("transport must be an object")
    transport_type = transport.get("type")
    if transport_type not in {None, "streamableHttp", "streamable-http", "http", "sse"}:
        raise ClineAdapterError("transport.type must be a supported remote transport")
    url = transport.get("url")
    if url is None:
        return None
    if not isinstance(url, str):
        raise ClineAdapterError("transport.url must be a string")
    return url


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ClineAdapterError(f"{field_name} must be a list of strings")
    return value
