from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from mcp_multiplex.certification import CertificationError, run_codex_certification
from mcp_multiplex.certification.claude_code import run_claude_code_certification
from mcp_multiplex.certification.cline import run_cline_certification
from mcp_multiplex.certification.gemini import (
    _assert_no_gemini_config_error,
    run_gemini_certification,
)
from mcp_multiplex.certification.opencode import run_opencode_certification
from mcp_multiplex.cli import main as cli_main
from mcp_multiplex.daemon import build_server


def test_codex_certification_requires_contract_port(tmp_path: Path) -> None:
    with pytest.raises(CertificationError, match="must use Hub data-plane port 30000"):
        run_codex_certification(work_dir=tmp_path, port=0)


def test_claude_code_certification_requires_contract_port(tmp_path: Path) -> None:
    with pytest.raises(CertificationError, match="must use Hub data-plane port 30000"):
        run_claude_code_certification(work_dir=tmp_path, port=0)


def test_gemini_certification_requires_contract_port(tmp_path: Path) -> None:
    with pytest.raises(CertificationError, match="must use Hub data-plane port 30000"):
        run_gemini_certification(work_dir=tmp_path, port=0)


def test_gemini_certification_rejects_invalid_config_warning() -> None:
    with pytest.raises(CertificationError, match="Gemini reported invalid configuration"):
        _assert_no_gemini_config_error(
            "Invalid configuration in settings.json:\nError in: mcpServers.mcp_hub.headers\n"
        )


def test_cline_certification_requires_contract_port(tmp_path: Path) -> None:
    with pytest.raises(CertificationError, match="must use Hub data-plane port 30000"):
        run_cline_certification(work_dir=tmp_path, port=0)


def test_opencode_certification_requires_contract_port(tmp_path: Path) -> None:
    with pytest.raises(CertificationError, match="must use Hub data-plane port 30000"):
        run_opencode_certification(work_dir=tmp_path, port=0)


def test_cli_certify_codex_reports_certification_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_certification(**_kwargs: object) -> object:
        raise CertificationError("cannot bind Hub certification server on port 30000")

    monkeypatch.setattr("mcp_multiplex.certification.run_codex_certification", fail_certification)

    exit_code = cli_main(["certify", "codex", "--work-dir", str(tmp_path)])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": 1,
        "kind": "MCPMultiplexCodexCertification",
        "ok": False,
        "error": {"detail": "cannot bind Hub certification server on port 30000"},
    }


def test_cli_certify_claude_code_reports_certification_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_certification(**_kwargs: object) -> object:
        raise CertificationError("Claude Code CLI not found: claude")

    monkeypatch.setattr(
        "mcp_multiplex.certification.run_claude_code_certification",
        fail_certification,
    )

    exit_code = cli_main(["certify", "claude-code", "--work-dir", str(tmp_path)])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": 1,
        "kind": "MCPMultiplexClaudeCodeCertification",
        "ok": False,
        "error": {"detail": "Claude Code CLI not found: claude"},
    }


def test_cli_certify_gemini_reports_certification_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_certification(**_kwargs: object) -> object:
        raise CertificationError("Gemini CLI not found: gemini")

    monkeypatch.setattr(
        "mcp_multiplex.certification.run_gemini_certification",
        fail_certification,
    )

    exit_code = cli_main(["certify", "gemini", "--work-dir", str(tmp_path)])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": 1,
        "kind": "MCPMultiplexGeminiCertification",
        "ok": False,
        "error": {"detail": "Gemini CLI not found: gemini"},
    }


def test_cli_certify_cline_reports_certification_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_certification(**_kwargs: object) -> object:
        raise CertificationError("Cline CLI not found: cline")

    monkeypatch.setattr(
        "mcp_multiplex.certification.run_cline_certification",
        fail_certification,
    )

    exit_code = cli_main(["certify", "cline", "--work-dir", str(tmp_path)])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": 1,
        "kind": "MCPMultiplexClineCertification",
        "ok": False,
        "error": {"detail": "Cline CLI not found: cline"},
    }


def test_cli_certify_opencode_reports_certification_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_certification(**_kwargs: object) -> object:
        raise CertificationError("OpenCode CLI not found: opencode")

    monkeypatch.setattr(
        "mcp_multiplex.certification.run_opencode_certification",
        fail_certification,
    )

    exit_code = cli_main(["certify", "opencode", "--work-dir", str(tmp_path)])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": 1,
        "kind": "MCPMultiplexOpenCodeCertification",
        "ok": False,
        "error": {"detail": "OpenCode CLI not found: opencode"},
    }


def test_cli_certify_codex_writes_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeResult:
        ok = True

        def to_dict(self) -> dict[str, Any]:
            return {
                "schema_version": 1,
                "kind": "MCPMultiplexCodexCertification",
                "ok": True,
                "steps": [],
            }

        def transcript(self) -> str:
            return "# redacted transcript\n"

    monkeypatch.setattr(
        "mcp_multiplex.certification.run_codex_certification",
        lambda **_kwargs: FakeResult(),
    )
    transcript = tmp_path / "checkpoint.md"

    exit_code = cli_main(["certify", "codex", "--transcript", str(transcript)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert transcript.read_text(encoding="utf-8") == "# redacted transcript\n"


def test_daemon_bind_failure_preserves_original_oserror() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])

        with pytest.raises(OSError, match="Address already in use"):
            build_server(port=port)
