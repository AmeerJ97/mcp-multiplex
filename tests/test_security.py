from __future__ import annotations

from typing import Any, cast

import pytest

from mcp_multiplex.schemas import CatalogEntry, ObservedEntry, ValidationError
from mcp_multiplex.security import (
    HUB_ORIGIN,
    SecurityError,
    validate_command_name,
    validate_http_url,
    validate_request_origin,
)
from tests.test_schema_models import catalog_entry_payload, observed_entry_payload


def test_browser_origin_is_denied_unless_explicitly_allowed() -> None:
    validate_request_origin(None)
    validate_request_origin(HUB_ORIGIN, allowed_origins={HUB_ORIGIN})

    with pytest.raises(SecurityError, match="browser origin is not allowed"):
        validate_request_origin("https://evil.example")


def test_command_name_rejects_shell_strings() -> None:
    assert validate_command_name("npx") == "npx"
    assert validate_command_name("/usr/local/bin/mcp-server") == "/usr/local/bin/mcp-server"

    with pytest.raises(SecurityError, match="not a shell string"):
        validate_command_name("npx -y @upstash/context7-mcp")
    with pytest.raises(SecurityError, match="not a shell string"):
        validate_command_name("node;curl https://example.invalid")


def test_http_url_validation_rejects_non_http_and_credential_bearing_urls() -> None:
    assert validate_http_url("https://example.com/mcp") == "https://example.com/mcp"

    with pytest.raises(SecurityError, match="http or https"):
        validate_http_url("file:///tmp/socket")
    with pytest.raises(SecurityError, match="must not include credentials"):
        validate_http_url("https://user:password@example.com/mcp")
    with pytest.raises(SecurityError, match="must not include a fragment"):
        validate_http_url("https://example.com/mcp#token")


def test_observed_entry_rejects_dangerous_stdio_command() -> None:
    payload = observed_entry_payload()
    payload["command"] = "npx -y @upstash/context7-mcp"

    with pytest.raises(ValidationError, match="not a shell string"):
        ObservedEntry.from_dict(payload)


def test_observed_entry_rejects_unsupported_http_url_scheme() -> None:
    payload = observed_entry_payload()
    payload.update(
        {
            "transport": "streamable_http",
            "command": None,
            "args": [],
            "url": "file:///tmp/mcp.sock",
        }
    )

    with pytest.raises(ValidationError, match="http or https"):
        ObservedEntry.from_dict(payload)


def test_catalog_backend_rejects_dangerous_command_and_bad_remote_url() -> None:
    command_payload = catalog_entry_payload()
    transport = dict(cast(dict[str, Any], command_payload["transport"]))
    backend = dict(cast(dict[str, Any], transport["backend"]))
    backend["command"] = "npx && curl https://example.invalid"
    transport["backend"] = backend
    command_payload["transport"] = transport

    with pytest.raises(ValidationError, match="not a shell string"):
        CatalogEntry.from_dict(command_payload)

    url_payload = catalog_entry_payload()
    url_transport = dict(cast(dict[str, Any], url_payload["transport"]))
    url_backend = dict(cast(dict[str, Any], url_transport["backend"]))
    url_backend.update(
        {"type": "streamable_http", "command": None, "args": [], "url": "ftp://example.com/mcp"}
    )
    url_transport["backend"] = url_backend
    url_payload["transport"] = url_transport
    runtime = dict(cast(dict[str, Any], url_payload["runtime"]))
    runtime["shareability"] = "isolated_per_frontend_session"
    url_payload["runtime"] = runtime

    with pytest.raises(ValidationError, match="http or https"):
        CatalogEntry.from_dict(url_payload)
