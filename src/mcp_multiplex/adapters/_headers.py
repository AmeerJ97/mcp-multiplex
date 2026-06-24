"""Header metadata helpers for agent config adapters."""

from __future__ import annotations

from typing import Any


def header_names_present(
    raw_entry: dict[str, Any],
    *,
    include_transport_headers: bool = False,
    include_bearer_token_env_var: bool = False,
) -> list[str]:
    """Return configured HTTP header names without retaining header values."""
    names: set[str] = set()
    if include_bearer_token_env_var and _non_empty_string(raw_entry.get("bearer_token_env_var")):
        names.add("Authorization")
    _collect_header_names(raw_entry.get("headers"), names)
    if include_transport_headers:
        transport = raw_entry.get("transport")
        if isinstance(transport, dict):
            _collect_header_names(transport.get("headers"), names)
    return sorted(names)


def _collect_header_names(value: Any, names: set[str]) -> None:
    if isinstance(value, dict):
        for key in value:
            if isinstance(key, str) and key:
                names.add(key)
        return
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, str):
                continue
            header_name = _header_name_from_string(item)
            if header_name:
                names.add(header_name)


def _header_name_from_string(value: str) -> str | None:
    separator_positions = [
        position for position in (value.find(":"), value.find("=")) if position > 0
    ]
    if not separator_positions:
        return None
    return value[: min(separator_positions)].strip() or None


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
