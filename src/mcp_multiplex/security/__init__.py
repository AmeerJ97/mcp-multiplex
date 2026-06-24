"""Security validation helpers for local control-plane and runtime inputs."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

HUB_ORIGIN = "http://127.0.0.1:30000"
HUB_URL_PREFIX = f"{HUB_ORIGIN}/servers/"
SHELL_META_PATTERN = re.compile(r"[\s;&|`$<>(){}\\\n\r]")
SUPPORTED_URL_SCHEMES = frozenset({"http", "https"})


class SecurityError(ValueError):
    """Raised when an input violates MCP Multiplex security policy."""


def validate_request_origin(origin: str | None, *, allowed_origins: set[str] | None = None) -> None:
    """Deny browser-origin requests unless the origin is explicitly allowed."""
    if not origin:
        return
    allowed = allowed_origins or set()
    if origin not in allowed:
        raise SecurityError(f"browser origin is not allowed: {origin}")


def validate_command_name(command: str | None, *, field_name: str = "command") -> str | None:
    """Reject shell-string command imports; args must stay in argv fields."""
    if command is None:
        return None
    if not command:
        raise SecurityError(f"{field_name} is required")
    if SHELL_META_PATTERN.search(command):
        raise SecurityError(f"{field_name} must be an executable name/path, not a shell string")
    return command


def validate_http_url(value: str | None, *, field_name: str = "url") -> str | None:
    """Validate HTTP(S) URLs and reject credential-bearing or ambiguous forms."""
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in SUPPORTED_URL_SCHEMES:
        raise SecurityError(f"{field_name} must use http or https")
    if not parsed.hostname:
        raise SecurityError(f"{field_name} must include a host")
    if parsed.username or parsed.password:
        raise SecurityError(f"{field_name} must not include credentials")
    if parsed.fragment:
        raise SecurityError(f"{field_name} must not include a fragment")
    return value


def is_hub_url(value: str | None) -> bool:
    """Return whether a URL follows the Hub-owned data-plane URL contract."""
    return bool(value and value.startswith(HUB_URL_PREFIX) and value.endswith("/mcp"))


__all__ = [
    "HUB_ORIGIN",
    "HUB_URL_PREFIX",
    "SecurityError",
    "is_hub_url",
    "validate_command_name",
    "validate_http_url",
    "validate_request_origin",
]
