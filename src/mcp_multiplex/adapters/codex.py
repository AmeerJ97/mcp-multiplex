"""Codex CLI config parser and observed-entry normalization."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_multiplex.adapters._headers import header_names_present
from mcp_multiplex.schemas import ObservedEntry

CODEX_AGENT_KIND = "codex"
CODEX_RAW_SHAPE = "codex-toml"
KNOWN_CODEX_ENTRY_KEYS = {
    "command",
    "args",
    "url",
    "env",
    "cwd",
    "enabled",
    "disabled",
    "enabled_tools",
    "disabled_tools",
    "approval_policy",
    "bearer_token_env_var",
}


class CodexAdapterError(ValueError):
    """Raised when Codex config cannot be parsed or normalized."""


@dataclass(frozen=True)
class ParsedCodexConfig:
    """Parsed Codex config with normalized observed entries."""

    config_path: str
    observed_entries: list[ObservedEntry]
    unsupported_fields: dict[str, dict[str, Any]] = field(default_factory=dict)

    def observed_dicts(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self.observed_entries]


def parse_codex_config(
    path: Path,
    *,
    agent_id: str = "agent_codex_user_default",
) -> ParsedCodexConfig:
    """Parse a Codex TOML config without mutating or rewriting it."""
    try:
        with path.open("rb") as file:
            parsed = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise CodexAdapterError(f"invalid Codex TOML at {path}: {error}") from error
    except OSError as error:
        raise CodexAdapterError(f"cannot read Codex config at {path}: {error}") from error

    servers = parsed.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise CodexAdapterError("Codex mcp_servers must be a TOML table")

    observed_entries: list[ObservedEntry] = []
    unsupported_fields: dict[str, dict[str, Any]] = {}
    for mount_name in sorted(servers):
        raw_entry = servers[mount_name]
        if not isinstance(raw_entry, dict):
            raise CodexAdapterError(f"Codex MCP entry {mount_name} must be a TOML table")
        unsupported = {
            key: value for key, value in raw_entry.items() if key not in KNOWN_CODEX_ENTRY_KEYS
        }
        if unsupported:
            unsupported_fields[str(mount_name)] = unsupported
        observed_entries.append(
            normalize_codex_entry(
                mount_name=str(mount_name),
                raw_entry=raw_entry,
                config_path=str(path),
                agent_id=agent_id,
                parser_confidence="partial" if unsupported else "complete",
            )
        )

    return ParsedCodexConfig(
        config_path=str(path),
        observed_entries=observed_entries,
        unsupported_fields=unsupported_fields,
    )


def normalize_codex_entry(
    *,
    mount_name: str,
    raw_entry: dict[str, Any],
    config_path: str,
    agent_id: str,
    parser_confidence: str,
) -> ObservedEntry:
    """Normalize one Codex MCP entry into the internal observed-entry schema."""
    url = _optional_string(raw_entry, "url")
    command = _optional_string(raw_entry, "command")
    if url:
        transport = "streamable_http"
    elif command:
        transport = "stdio"
    else:
        raise CodexAdapterError(f"Codex MCP entry {mount_name} needs command or url")

    args = _string_list(raw_entry.get("args", []), field_name=f"{mount_name}.args")
    env = raw_entry.get("env", {})
    if env is None:
        env = {}
    if not isinstance(env, dict):
        raise CodexAdapterError(f"Codex MCP entry {mount_name}.env must be a table")
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
        "agent_kind": CODEX_AGENT_KIND,
        "config_path": config_path,
        "container_path": ["mcp_servers", mount_name],
        "mount_name": mount_name,
        "enabled": enabled,
        "transport": transport,
        "command": command,
        "args": args,
        "url": url,
        "headers_present": header_names_present(raw_entry, include_bearer_token_env_var=True),
        "env_names": env_names,
        "cwd": _optional_string(raw_entry, "cwd"),
        "tool_filters": {"enabled_tools": enabled_tools, "disabled_tools": disabled_tools},
        "approval_policy": _optional_string(raw_entry, "approval_policy"),
        "entry_hash": entry_hash(mount_name, raw_entry),
        "raw_shape": CODEX_RAW_SHAPE,
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


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _optional_string(raw_entry: dict[str, Any], key: str) -> str | None:
    value = raw_entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CodexAdapterError(f"{key} must be a string")
    return value


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CodexAdapterError(f"{field_name} must be a list of strings")
    return value
