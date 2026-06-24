from __future__ import annotations

import json
import threading
from collections.abc import Generator
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.request import urlopen

import pytest

from mcp_multiplex.cli import main as cli_main
from mcp_multiplex.daemon import build_server
from mcp_multiplex.health import healthy_payload, is_health_payload


@pytest.fixture
def health_server() -> Generator[ThreadingHTTPServer]:
    server = build_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def server_port(server: ThreadingHTTPServer) -> int:
    return int(server.server_address[1])


def test_healthy_payload_matches_stable_schema() -> None:
    payload = healthy_payload()

    assert is_health_payload(payload)
    assert payload == {
        "schema_version": 1,
        "kind": "MCPMultiplexHealth",
        "ok": True,
        "summary": {
            "agents": 0,
            "blockers": 0,
            "warnings": 0,
            "notices": 0,
            "active_servers": 0,
            "hot_backends": 0,
            "pending_approvals": 0,
        },
        "blockers": [],
        "warnings": [],
        "notices": [],
    }


def test_daemon_health_endpoint_returns_payload(health_server: ThreadingHTTPServer) -> None:
    url = f"http://127.0.0.1:{server_port(health_server)}/healthz"

    with urlopen(url, timeout=2) as response:
        payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))

    assert response.status == 200
    assert is_health_payload(payload)
    assert payload["ok"] is True


def test_cli_health_reports_daemon_health(
    health_server: ThreadingHTTPServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_main(["health", "--port", str(server_port(health_server))])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert is_health_payload(payload)
    assert payload["ok"] is True


def test_cli_health_reports_unavailable_daemon(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main(["health", "--port", "1", "--timeout", "0.1"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert is_health_payload(payload)
    assert payload["ok"] is False
    assert payload["summary"]["blockers"] == 1
    assert payload["blockers"][0]["area"] == "daemon"
    assert payload["blockers"][0]["code"] == "daemon_unavailable"
