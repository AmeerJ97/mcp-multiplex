"""OpenCode config parser and observed-entry normalization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_multiplex.adapters._headers import header_names_present
from mcp_multiplex.schemas import ObservedEntry

OPENCODE_AGENT_KIND = "opencode"
OPENCODE_RAW_SHAPE = "opencode-json"
KNOWN_OPENCODE_ENTRY_KEYS = {
    "command",
    "args",
    "url",
    "type",
    "env",
    "cwd",
    "enabled",
    "disabled",
    "enabled_tools",
    "disabled_tools",
    "approval_policy",
    "headers",
    "oauth",
}


class OpenCodeAdapterError(ValueError):
    """Raised when OpenCode config cannot be parsed or normalized."""


@dataclass(frozen=True)
class ParsedOpenCodeConfig:
    """Parsed OpenCode config with normalized observed entries."""

    config_path: str
    observed_entries: list[ObservedEntry]
    unsupported_fields: dict[str, dict[str, Any]] = field(default_factory=dict)

    def observed_dicts(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self.observed_entries]


def parse_opencode_config(
    path: Path,
    *,
    agent_id: str = "agent_opencode_user_default",
) -> ParsedOpenCodeConfig:
    """Parse an OpenCode JSON/JSONC config without mutating or rewriting it."""
    try:
        config_text = path.read_text(encoding="utf-8")
        parsed = json.loads(_strip_jsonc_comments(config_text))
    except json.JSONDecodeError as error:
        raise OpenCodeAdapterError(f"invalid OpenCode JSON at {path}: {error}") from error
    except OSError as error:
        message = f"cannot read OpenCode config at {path}: {error}"
        raise OpenCodeAdapterError(message) from error

    if not isinstance(parsed, dict):
        raise OpenCodeAdapterError("OpenCode config root must be an object")
    container_key = "mcp" if "mcp" in parsed else "mcpServers"
    servers = parsed.get(container_key, {})
    if not isinstance(servers, dict):
        raise OpenCodeAdapterError(f"OpenCode {container_key} must be an object")

    observed_entries: list[ObservedEntry] = []
    unsupported_fields: dict[str, dict[str, Any]] = {}
    for mount_name in sorted(servers):
        raw_entry = servers[mount_name]
        if not isinstance(raw_entry, dict):
            raise OpenCodeAdapterError(f"OpenCode MCP entry {mount_name} must be an object")
        unsupported = {
            key: value for key, value in raw_entry.items() if key not in KNOWN_OPENCODE_ENTRY_KEYS
        }
        if unsupported:
            unsupported_fields[str(mount_name)] = unsupported
        observed_entries.append(
            normalize_opencode_entry(
                mount_name=str(mount_name),
                raw_entry=raw_entry,
                config_path=str(path),
                agent_id=agent_id,
                parser_confidence="partial" if unsupported else "complete",
                container_key=container_key,
            )
        )

    return ParsedOpenCodeConfig(
        config_path=str(path),
        observed_entries=observed_entries,
        unsupported_fields=unsupported_fields,
    )


def normalize_opencode_entry(
    *,
    mount_name: str,
    raw_entry: dict[str, Any],
    config_path: str,
    agent_id: str,
    parser_confidence: str,
    container_key: str = "mcpServers",
) -> ObservedEntry:
    """Normalize one OpenCode MCP entry into the observed-entry schema."""
    url = _optional_string(raw_entry, "url")
    command, args = _command_and_args(raw_entry, mount_name=mount_name)
    if url:
        transport = "streamable_http"
    elif command:
        transport = "stdio"
    else:
        raise OpenCodeAdapterError(f"OpenCode MCP entry {mount_name} needs command or url")

    env = raw_entry.get("env", {})
    if env is None:
        env = {}
    if not isinstance(env, dict):
        raise OpenCodeAdapterError(f"OpenCode MCP entry {mount_name}.env must be an object")
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
        "agent_kind": OPENCODE_AGENT_KIND,
        "config_path": config_path,
        "container_path": [container_key, mount_name],
        "mount_name": mount_name,
        "enabled": enabled,
        "transport": transport,
        "command": command,
        "args": args,
        "url": url,
        "headers_present": header_names_present(raw_entry),
        "env_names": env_names,
        "cwd": _optional_string(raw_entry, "cwd"),
        "tool_filters": {"enabled_tools": enabled_tools, "disabled_tools": disabled_tools},
        "approval_policy": _optional_string(raw_entry, "approval_policy"),
        "entry_hash": entry_hash(mount_name, raw_entry),
        "raw_shape": OPENCODE_RAW_SHAPE,
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


def _strip_jsonc_comments(config_text: str) -> str:
    result: list[str] = []
    index = 0
    in_string = False
    escape = False
    while index < len(config_text):
        char = config_text[index]
        next_char = config_text[index + 1] if index + 1 < len(config_text) else ""
        if in_string:
            result.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(config_text) and config_text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(config_text) and config_text[index : index + 2] != "*/":
                result.append("\n" if config_text[index] in "\r\n" else " ")
                index += 1
            index += 2 if index + 1 < len(config_text) else 0
            continue
        result.append(char)
        index += 1
    return "".join(result)


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
        raise OpenCodeAdapterError(f"{key} must be a string")
    return value


def _command_and_args(
    raw_entry: dict[str, Any],
    *,
    mount_name: str,
) -> tuple[str | None, list[str]]:
    command = raw_entry.get("command")
    if command is None:
        return None, _string_list(raw_entry.get("args", []), field_name=f"{mount_name}.args")
    if isinstance(command, str):
        args = _string_list(raw_entry.get("args", []), field_name=f"{mount_name}.args")
        return command, args
    if isinstance(command, list) and all(isinstance(item, str) for item in command):
        if not command:
            raise OpenCodeAdapterError(f"{mount_name}.command must not be empty")
        return command[0], command[1:]
    raise OpenCodeAdapterError(f"{mount_name}.command must be a string or list of strings")


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OpenCodeAdapterError(f"{field_name} must be a list of strings")
    return value
