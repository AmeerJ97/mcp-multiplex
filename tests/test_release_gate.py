from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_multiplex.cli as cli_module
from mcp_multiplex.adapters import AgentRegistry, parse_codex_config
from mcp_multiplex.catalog import CatalogStore
from mcp_multiplex.cli import main as cli_main
from mcp_multiplex.observability import EventStore, ObservedEntryStore
from mcp_multiplex.runtime import RuntimeBackendStore
from mcp_multiplex.schemas import CatalogEntry
from mcp_multiplex.storage import connect, migrate
from tests.test_catalog_matching import catalog_entry


def test_release_gate_fails_on_active_direct_bypass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "@upstash/context7-mcp"]\n',
        encoding="utf-8",
    )
    ObservedEntryStore(connection).upsert_many(parse_codex_config(config_path).observed_entries)

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(_passing_checkpoints(tmp_path)),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert _check(payload, "no_active_direct_bypass")["ok"] is False


def test_release_gate_fails_on_failed_real_client_certification(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkpoint_dir = _passing_checkpoints(tmp_path)
    (checkpoint_dir / "TASK-040-gemini-certification.md").write_text(
        "# TASK-040 Gemini CLI Certification\n\nResult: FAIL\n",
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--db-path",
            str(tmp_path / "multiplex.db"),
            "--checkpoint-dir",
            str(checkpoint_dir),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert _check(payload, "real_client_certifications")["ok"] is False


def test_release_gate_fails_on_unauthenticated_mcp_hub_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    AgentRegistry(connection).create(
        agent_id="agent_codex_user_default",
        agent_kind="codex",
        display_name="Codex CLI",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[mcp_servers.mcp_hub]\nurl = "http://127.0.0.1:30000/servers/mcp_hub/mcp"\n',
        encoding="utf-8",
    )
    ObservedEntryStore(connection).upsert_many(parse_codex_config(config_path).observed_entries)

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(_passing_checkpoints(tmp_path)),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    check = _check(payload, "mcp_hub_auth")
    assert check["ok"] is False
    findings = check["findings"]
    assert isinstance(findings, list)
    first_finding = findings[0]
    assert isinstance(first_finding, dict)
    assert first_finding["detail"] == (
        "mcp_hub control-plane entry lacks Authorization header metadata"
    )


def test_release_gate_fails_on_secret_value_in_audit_log(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    _insert_event_with_payload(connection, {"api_key": "sk-test1234567890abcdef"})

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(_passing_checkpoints(tmp_path)),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert _check(payload, "audit_secret_redaction")["ok"] is False


def test_release_gate_allows_token_ref_in_audit_log(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    EventStore(connection).append(
        event_id="evt_token_ref",
        event_type="auth.token_issued",
        actor="test",
        result="success",
        payload={"token_ref": "tokref_agent_example"},
        timestamp="2026-06-21T00:00:00Z",
    )

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(_passing_checkpoints(tmp_path)),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert _check(payload, "audit_secret_redaction")["ok"] is True


def test_release_gate_fails_when_non_shareable_backend_is_shared(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    payload = catalog_entry().to_dict()
    payload["runtime"]["shareability"] = "per_agent"
    CatalogStore(connection).upsert(CatalogEntry.from_dict(payload))
    RuntimeBackendStore(connection).create_starting(
        catalog_id="srv_context7",
        runtime_pool_key="global:catalog:srv_context7",
        pid=123,
    )

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(_passing_checkpoints(tmp_path)),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert _check(payload, "runtime_share_policy")["ok"] is False


def test_global_release_gate_fails_without_daemon_service_and_legacy_catalog(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--global-cutover",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(_passing_checkpoints(tmp_path)),
            "--daemon-unit-dir",
            str(tmp_path / "systemd-user"),
            "--daemon-systemctl-bin",
            str(tmp_path / "missing-systemctl"),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "global_cutover"
    assert _check(payload, "daemon_user_service")["ok"] is False
    assert _check(payload, "legacy_catalog_import")["ok"] is False


def test_global_release_gate_requires_approved_legacy_catalog_entries(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    _insert_legacy_catalog_entry(connection, review_state="pending")
    _insert_legacy_catalog_import_event(connection)
    checkpoint_dir = _passing_checkpoints(tmp_path)
    _import_certification_evidence(db_path, checkpoint_dir, capsys)
    unit_dir = _installed_unit_dir(tmp_path)
    systemctl = _fake_systemctl_bin(tmp_path)

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--global-cutover",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--daemon-unit-dir",
            str(unit_dir),
            "--daemon-systemctl-bin",
            str(systemctl),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    legacy_check = _check(payload, "legacy_catalog_import")
    assert legacy_check["ok"] is False
    findings = legacy_check["findings"]
    assert isinstance(findings, list)
    assert findings[0]["detail"] == "legacy imported catalog entry is not approved"


def test_global_release_gate_requires_hash_bound_certification_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    _insert_legacy_catalog_entry(connection, review_state="approved")
    _insert_legacy_catalog_import_event(connection)
    unit_dir = _installed_unit_dir(tmp_path)
    systemctl = _fake_systemctl_bin(tmp_path)
    checkpoint_dir = _passing_checkpoints(tmp_path)

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--global-cutover",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--daemon-unit-dir",
            str(unit_dir),
            "--daemon-systemctl-bin",
            str(systemctl),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    evidence_check = _check(payload, "certification_evidence_hashes")
    assert evidence_check["ok"] is False
    findings = evidence_check["findings"]
    assert isinstance(findings, list)
    assert findings[0]["detail"] == "missing hash-bound certification evidence event"


def test_global_release_gate_requires_implemented_safe_control_plane_installers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    _insert_legacy_catalog_entry(connection, review_state="approved")
    _insert_legacy_catalog_import_event(connection)
    checkpoint_dir = _passing_checkpoints(tmp_path)
    _import_certification_evidence(db_path, checkpoint_dir, capsys)
    unit_dir = _installed_unit_dir(tmp_path)
    systemctl = _fake_systemctl_bin(tmp_path)

    unsupported = SimpleNamespace(
        agent_kind="cline",
        automatic_install_supported=False,
        status="blocked",
        raw_token_storage_required=True,
        to_dict=lambda: {
            "agent_kind": "cline",
            "automatic_install_supported": False,
            "status": "blocked",
            "raw_token_storage_required": True,
        },
    )
    monkeypatch.setattr(cli_module, "control_plane_auth_capabilities", lambda: [unsupported])

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--global-cutover",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--daemon-unit-dir",
            str(unit_dir),
            "--daemon-systemctl-bin",
            str(systemctl),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    capability_check = _check(payload, "control_plane_auth_capabilities")
    assert capability_check["ok"] is False
    findings = capability_check["findings"]
    assert isinstance(findings, list)
    assert {finding["detail"] for finding in findings} == {
        "automatic authenticated control-plane install is not supported",
        "authenticated control-plane installer is not implemented",
        "authenticated control-plane install requires raw token storage",
    }


def test_certify_import_evidence_records_checkpoint_hashes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    checkpoint_dir = _passing_checkpoints(tmp_path)

    _import_certification_evidence(db_path, checkpoint_dir, capsys)

    events = EventStore(connect(db_path)).query(event_type="certification.evidence_imported")
    assert len(events) == 5
    codex_event = next(event for event in events if event.payload["client"] == "codex")
    codex_path = checkpoint_dir / "TASK-038-codex-certification.md"
    assert codex_event.payload["transcript_hash"] == _test_file_digest(codex_path)
    assert codex_event.event.after_hash == _test_file_digest(codex_path)


def test_global_release_gate_fails_when_checkpoint_changes_after_evidence_import(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    _insert_legacy_catalog_entry(connection, review_state="approved")
    _insert_legacy_catalog_import_event(connection)
    unit_dir = _installed_unit_dir(tmp_path)
    systemctl = _fake_systemctl_bin(tmp_path)
    checkpoint_dir = _passing_checkpoints(tmp_path)
    _import_certification_evidence(db_path, checkpoint_dir, capsys)
    (checkpoint_dir / "TASK-038-codex-certification.md").write_text(
        "# cert\n\nResult: PASS\n\nmutated after import\n",
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--global-cutover",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--daemon-unit-dir",
            str(unit_dir),
            "--daemon-systemctl-bin",
            str(systemctl),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    evidence_check = _check(payload, "certification_evidence_hashes")
    assert evidence_check["ok"] is False
    findings = evidence_check["findings"]
    assert isinstance(findings, list)
    assert findings[0]["client"] == "codex"
    assert findings[0]["detail"] == "checkpoint hash does not match imported evidence"


def test_global_release_gate_passes_with_daemon_and_reviewed_legacy_catalog(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    _insert_legacy_catalog_entry(connection, review_state="approved")
    _insert_legacy_catalog_import_event(connection)
    checkpoint_dir = _passing_checkpoints(tmp_path)
    _import_certification_evidence(db_path, checkpoint_dir, capsys)
    unit_dir = _installed_unit_dir(tmp_path)
    systemctl = _fake_systemctl_bin(tmp_path)

    exit_code = cli_main(
        [
            "doctor",
            "release-gate",
            "--global-cutover",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--daemon-unit-dir",
            str(unit_dir),
            "--daemon-systemctl-bin",
            str(systemctl),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert _check(payload, "control_plane_auth_capabilities")["ok"] is True
    assert _check(payload, "certification_evidence_hashes")["ok"] is True
    assert _check(payload, "daemon_user_service")["ok"] is True
    assert _check(payload, "legacy_catalog_import")["ok"] is True


def test_cutover_apply_requires_explicit_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_main(
        [
            "cutover",
            "apply",
            "--from",
            "mcp-hub",
            "--db-path",
            str(tmp_path / "multiplex.db"),
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexCutoverApply"
    assert payload["ok"] is False
    assert "confirm" in payload["error"]["detail"]


def test_cutover_apply_fails_closed_when_global_gate_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"

    exit_code = cli_main(
        [
            "cutover",
            "apply",
            "--from",
            "mcp-hub",
            "--confirm-retire-mcp-hub",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(_passing_checkpoints(tmp_path)),
            "--daemon-unit-dir",
            str(tmp_path / "systemd-user"),
            "--daemon-systemctl-bin",
            str(tmp_path / "missing-systemctl"),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexCutoverApply"
    assert payload["ok"] is False
    assert payload["release_gate"]["ok"] is False
    assert EventStore(connect(db_path)).query(event_type="cutover.applied") == []


def test_cutover_apply_records_retirement_after_global_gate_passes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    _insert_legacy_catalog_entry(connection, review_state="approved")
    _insert_legacy_catalog_import_event(connection)
    checkpoint_dir = _passing_checkpoints(tmp_path)
    _import_certification_evidence(db_path, checkpoint_dir, capsys)
    unit_dir = _installed_unit_dir(tmp_path)
    systemctl = _fake_systemctl_bin(tmp_path)

    exit_code = cli_main(
        [
            "cutover",
            "apply",
            "--from",
            "mcp-hub",
            "--confirm-retire-mcp-hub",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--daemon-unit-dir",
            str(unit_dir),
            "--daemon-systemctl-bin",
            str(systemctl),
            "--actor",
            "test_operator",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexCutoverApply"
    assert payload["ok"] is True
    assert payload["release_gate"]["ok"] is True
    assert payload["result"]["legacy_mcp_hub_deprecated"] is True
    assert payload["result"]["unmanaged_process_action"] == "none"
    events = EventStore(connection).query(event_type="cutover.applied")
    assert len(events) == 1
    assert events[0].event.actor == "test_operator"
    assert events[0].payload["legacy_mcp_hub_deprecated"] is True
    assert events[0].payload["unmanaged_process_action"] == "none"


def test_cutover_status_reports_missing_retirement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_main(["cutover", "status", "--db-path", str(tmp_path / "multiplex.db")])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexCutoverStatus"
    assert payload["ok"] is False
    assert payload["legacy_mcp_hub_deprecated"] is False
    assert payload["latest_event"] is None
    assert payload["release_gate"] is None


def test_cutover_status_reports_retirement_and_current_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    _insert_legacy_catalog_entry(connection, review_state="approved")
    _insert_legacy_catalog_import_event(connection)
    checkpoint_dir = _passing_checkpoints(tmp_path)
    _import_certification_evidence(db_path, checkpoint_dir, capsys)
    unit_dir = _installed_unit_dir(tmp_path)
    systemctl = _fake_systemctl_bin(tmp_path)
    EventStore(connection).append(
        event_id="evt_cutover_applied_status_test",
        event_type="cutover.applied",
        actor="test_operator",
        result="success",
        payload={
            "source": "mcp-hub",
            "legacy_mcp_hub_deprecated": True,
            "unmanaged_process_action": "none",
        },
        timestamp="2026-06-22T00:00:00Z",
    )

    exit_code = cli_main(
        [
            "cutover",
            "status",
            "--from",
            "mcp-hub",
            "--check-gate",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--daemon-unit-dir",
            str(unit_dir),
            "--daemon-systemctl-bin",
            str(systemctl),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexCutoverStatus"
    assert payload["ok"] is True
    assert payload["legacy_mcp_hub_deprecated"] is True
    assert payload["unmanaged_process_action"] == "none"
    assert payload["latest_event"]["event_id"] == "evt_cutover_applied_status_test"
    assert payload["latest_event"]["payload"]["legacy_mcp_hub_deprecated"] is True
    assert payload["release_gate"]["ok"] is True
    assert payload["release_gate"]["mode"] == "global_cutover"


def test_cutover_status_can_include_clean_legacy_footprint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    EventStore(connection).append(
        event_id="evt_cutover_applied_clean_footprint_test",
        event_type="cutover.applied",
        actor="test_operator",
        result="success",
        payload={
            "source": "mcp-hub",
            "legacy_mcp_hub_deprecated": True,
            "unmanaged_process_action": "none",
        },
        timestamp="2026-06-22T00:00:00Z",
    )
    legacy_root = tmp_path / "missing-mcp-hub"
    home = tmp_path / "home"
    ps_bin = _fake_ps_bin(tmp_path, "  10   1 python /usr/bin/python app.py\n")

    exit_code = cli_main(
        [
            "cutover",
            "status",
            "--check-footprint",
            "--db-path",
            str(db_path),
            "--legacy-root",
            str(legacy_root),
            "--home",
            str(home),
            "--ps-bin",
            str(ps_bin),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["legacy_footprint"]["ok"] is True
    assert payload["legacy_footprint"]["legacy_root_exists"] is False
    assert payload["legacy_footprint"]["process_scan"]["match_count"] == 0


def test_cutover_status_fails_when_checked_legacy_footprint_has_active_process(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    EventStore(connection).append(
        event_id="evt_cutover_applied_dirty_footprint_test",
        event_type="cutover.applied",
        actor="test_operator",
        result="success",
        payload={
            "source": "mcp-hub",
            "legacy_mcp_hub_deprecated": True,
            "unmanaged_process_action": "none",
        },
        timestamp="2026-06-22T00:00:00Z",
    )
    legacy_root = tmp_path / "mcp-hub"
    legacy_root.mkdir()
    ps_bin = _fake_ps_bin(
        tmp_path,
        "  12   1 python /opt/legacy/mcp-hub/launch-hub.py\n",
    )

    exit_code = cli_main(
        [
            "cutover",
            "status",
            "--check-footprint",
            "--db-path",
            str(db_path),
            "--legacy-root",
            str(legacy_root),
            "--ps-bin",
            str(ps_bin),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["legacy_mcp_hub_deprecated"] is True
    assert payload["legacy_footprint"]["ok"] is False
    assert payload["legacy_footprint"]["process_scan"]["match_count"] == 1
    assert payload["legacy_footprint"]["unmanaged_process_action"] == "none"


def test_cutover_legacy_footprint_flags_repo_and_service_without_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy_root = tmp_path / "mcp-hub"
    legacy_root.mkdir()
    (legacy_root / ".git").mkdir()
    (legacy_root / "hub.json.bak-20260622T000000Z").write_text("{}", encoding="utf-8")
    (legacy_root / "launch-hub.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "mcp-hub.service").write_text("[Service]\nExecStart=mcp-hub\n", encoding="utf-8")
    ps_bin = _fake_ps_bin(tmp_path, "  10   1 python /usr/bin/python app.py\n")

    exit_code = cli_main(
        [
            "cutover",
            "legacy-footprint",
            "--legacy-root",
            str(legacy_root),
            "--home",
            str(home),
            "--ps-bin",
            str(ps_bin),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexLegacyFootprint"
    assert payload["ok"] is False
    assert payload["legacy_root_exists"] is True
    assert payload["legacy_root_git_repository"] is True
    assert payload["catalog_exports"] == ["hub.json.bak-20260622T000000Z"]
    assert payload["service_units"][0]["name"] == "mcp-hub.service"
    assert payload["process_scan"]["match_count"] == 0
    assert payload["unmanaged_process_action"] == "none"
    action_kinds = {action["kind"] for action in payload["operator_actions_required"]}
    assert action_kinds == {"legacy_repo_present", "legacy_service_unit_present"}


def test_cutover_legacy_footprint_passes_when_no_legacy_evidence_remains(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy_root = tmp_path / "missing-mcp-hub"
    home = tmp_path / "home"
    ps_bin = _fake_ps_bin(tmp_path, "  10   1 python /usr/bin/python app.py\n")

    exit_code = cli_main(
        [
            "cutover",
            "legacy-footprint",
            "--legacy-root",
            str(legacy_root),
            "--home",
            str(home),
            "--ps-bin",
            str(ps_bin),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["legacy_root_exists"] is False
    assert payload["service_units"] == []
    assert payload["legacy_executables"] == []
    assert payload["process_scan"]["match_count"] == 0
    assert payload["operator_actions_required"] == []


def test_cutover_legacy_footprint_flags_resolvable_legacy_executable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_root = tmp_path / "missing-mcp-hub"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "mcp-hub"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    ps_bin = _fake_ps_bin(tmp_path, "  10   1 python /usr/bin/python app.py\n")

    exit_code = cli_main(
        [
            "cutover",
            "legacy-cleanup-plan",
            "--legacy-root",
            str(legacy_root),
            "--home",
            str(home),
            "--ps-bin",
            str(ps_bin),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["footprint"]["legacy_executables"] == [
        {"name": "mcp-hub", "path": str(executable)}
    ]
    assert payload["step_count"] == 1
    assert payload["steps"][0]["step_id"] == "uninstall_legacy_executables"
    assert payload["mutation_action"] == "none"


def test_cutover_legacy_footprint_flags_active_process_without_killing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy_root = tmp_path / "mcp-hub"
    legacy_root.mkdir()
    ps_bin = _fake_ps_bin(
        tmp_path,
        "  11   1 python /opt/mcp-multiplex/.venv/bin/mcp-multiplex\n"
        "  12   1 python /opt/legacy/mcp-hub/launch-hub.py\n",
    )

    exit_code = cli_main(
        [
            "cutover",
            "legacy-footprint",
            "--legacy-root",
            str(legacy_root),
            "--ps-bin",
            str(ps_bin),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["process_scan"]["match_count"] == 1
    assert payload["process_scan"]["matches"][0]["pid"] == 12
    assert payload["unmanaged_process_action"] == "none"
    assert payload["operator_actions_required"][-1]["kind"] == "legacy_process_present"


def test_cutover_legacy_cleanup_plan_reports_explicit_operator_steps(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy_root = tmp_path / "mcp-hub"
    legacy_root.mkdir()
    (legacy_root / ".git").mkdir()
    (legacy_root / "hub.json.bak-20260622T000000Z").write_text("{}", encoding="utf-8")
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "mcp-hub.service").write_text("[Service]\nExecStart=mcp-hub\n", encoding="utf-8")
    ps_bin = _fake_ps_bin(
        tmp_path,
        "  12   1 python /opt/legacy/mcp-hub/launch-hub.py\n",
    )

    exit_code = cli_main(
        [
            "cutover",
            "legacy-cleanup-plan",
            "--legacy-root",
            str(legacy_root),
            "--home",
            str(home),
            "--ps-bin",
            str(ps_bin),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexLegacyCleanupPlan"
    assert payload["ok"] is False
    assert payload["apply_supported"] is False
    assert payload["mutation_action"] == "none"
    assert payload["unmanaged_process_action"] == "none"
    assert payload["step_count"] == 3
    assert {step["step_id"] for step in payload["steps"]} == {
        "archive_or_remove_legacy_repo",
        "disable_legacy_service_units",
        "stop_legacy_processes",
    }
    assert all(step["approval_required"] is True for step in payload["steps"])
    assert all(step["destructive"] is True for step in payload["steps"])
    process_step = next(
        step for step in payload["steps"] if step["step_id"] == "stop_legacy_processes"
    )
    assert process_step["evidence"]["match_count"] == 1


def test_cutover_legacy_cleanup_plan_passes_when_no_steps_remain(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy_root = tmp_path / "missing-mcp-hub"
    home = tmp_path / "home"
    ps_bin = _fake_ps_bin(tmp_path, "  10   1 python /usr/bin/python app.py\n")

    exit_code = cli_main(
        [
            "cutover",
            "legacy-cleanup-plan",
            "--legacy-root",
            str(legacy_root),
            "--home",
            str(home),
            "--ps-bin",
            str(ps_bin),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["steps"] == []
    assert payload["step_count"] == 0
    assert payload["apply_supported"] is False


def test_doctor_retirement_gate_passes_after_cutover_with_clean_footprint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    _insert_legacy_catalog_entry(connection, review_state="approved")
    _insert_legacy_catalog_import_event(connection)
    checkpoint_dir = _passing_checkpoints(tmp_path)
    _import_certification_evidence(db_path, checkpoint_dir, capsys)
    unit_dir = _installed_unit_dir(tmp_path)
    systemctl = _fake_systemctl_bin(tmp_path)
    EventStore(connection).append(
        event_id="evt_retirement_gate_cutover",
        event_type="cutover.applied",
        actor="test_operator",
        result="success",
        payload={
            "source": "mcp-hub",
            "legacy_mcp_hub_deprecated": True,
            "unmanaged_process_action": "none",
        },
        timestamp="2026-06-22T00:00:00Z",
    )
    home = tmp_path / "home"
    legacy_root = tmp_path / "missing-mcp-hub"
    ps_bin = _fake_ps_bin(tmp_path, "  10   1 python /usr/bin/python app.py\n")

    exit_code = cli_main(
        [
            "doctor",
            "retirement-gate",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--daemon-unit-dir",
            str(unit_dir),
            "--daemon-systemctl-bin",
            str(systemctl),
            "--home",
            str(home),
            "--legacy-root",
            str(legacy_root),
            "--ps-bin",
            str(ps_bin),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexRetirementGate"
    assert payload["ok"] is True
    assert {check["name"]: check["ok"] for check in payload["checks"]} == {
        "global_release_gate": True,
        "cutover_applied": True,
        "legacy_footprint_clean": True,
    }
    assert payload["mutation_action"] == "none"
    assert payload["unmanaged_process_action"] == "none"


def test_doctor_retirement_gate_fails_when_legacy_footprint_is_dirty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "multiplex.db"
    connection = connect(db_path)
    _insert_legacy_catalog_entry(connection, review_state="approved")
    _insert_legacy_catalog_import_event(connection)
    checkpoint_dir = _passing_checkpoints(tmp_path)
    _import_certification_evidence(db_path, checkpoint_dir, capsys)
    unit_dir = _installed_unit_dir(tmp_path)
    systemctl = _fake_systemctl_bin(tmp_path)
    EventStore(connection).append(
        event_id="evt_retirement_gate_dirty_cutover",
        event_type="cutover.applied",
        actor="test_operator",
        result="success",
        payload={
            "source": "mcp-hub",
            "legacy_mcp_hub_deprecated": True,
            "unmanaged_process_action": "none",
        },
        timestamp="2026-06-22T00:00:00Z",
    )
    home = tmp_path / "home"
    legacy_root = tmp_path / "mcp-hub"
    legacy_root.mkdir()
    ps_bin = _fake_ps_bin(
        tmp_path,
        "  12   1 python /opt/legacy/mcp-hub/launch-hub.py\n",
    )

    exit_code = cli_main(
        [
            "doctor",
            "retirement-gate",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--daemon-unit-dir",
            str(unit_dir),
            "--daemon-systemctl-bin",
            str(systemctl),
            "--home",
            str(home),
            "--legacy-root",
            str(legacy_root),
            "--ps-bin",
            str(ps_bin),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["global_release_gate"]["ok"] is True
    assert checks["cutover_applied"]["ok"] is True
    assert checks["legacy_footprint_clean"]["ok"] is False
    cleanup_plan = checks["legacy_footprint_clean"]["cleanup_plan"]
    assert cleanup_plan["apply_supported"] is False
    assert cleanup_plan["step_count"] == 2


def test_migration_dry_run_parses_legacy_configs_without_mutating(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy_root = tmp_path / "legacy-home"
    codex_dir = legacy_root / ".codex"
    codex_dir.mkdir(parents=True)
    config_path = codex_dir / "config.toml"
    config_path.write_text(
        '[mcp_servers.context7]\nurl = "http://127.0.0.1:30000/servers/context7/mcp"\n',
        encoding="utf-8",
    )
    before = config_path.read_bytes()

    exit_code = cli_main(["doctor", "migration-dry-run", "--legacy-root", str(legacy_root)])

    assert exit_code == 0
    assert config_path.read_bytes() == before
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["observed_count"] == 1
    assert payload["mutated_paths"] == []
    assert payload["classifications"][0]["classification"] == "compliant_hub_routed"


def test_cutover_dry_run_wraps_migration_analysis_without_mutating(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy_root = tmp_path / "legacy-home"
    codex_dir = legacy_root / ".codex"
    codex_dir.mkdir(parents=True)
    config_path = codex_dir / "config.toml"
    config_path.write_text(
        '[mcp_servers.context7]\nurl = "http://127.0.0.1:30000/servers/context7/mcp"\n',
        encoding="utf-8",
    )
    before = config_path.read_bytes()

    exit_code = cli_main(
        ["cutover", "dry-run", "--from", "mcp-hub", "--legacy-root", str(legacy_root)]
    )

    assert exit_code == 0
    assert config_path.read_bytes() == before
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexCutoverDryRun"
    assert payload["source"] == "mcp-hub"
    assert payload["apply_supported"] is False
    assert payload["mutated_paths"] == []
    assert payload["classifications"][0]["classification"] == "compliant_hub_routed"


def test_cutover_dry_run_rejects_unknown_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_main(["cutover", "dry-run", "--from", "other", "--legacy-root", str(tmp_path)])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexCutoverDryRun"
    assert payload["ok"] is False
    assert payload["error"]["detail"] == "unsupported cutover source: other"


def _passing_checkpoints(tmp_path: Path) -> Path:
    checkpoint_dir = tmp_path / "checkpoints"
    checkpoint_dir.mkdir(exist_ok=True)
    for filename in (
        "TASK-038-codex-certification.md",
        "TASK-039-claude-code-certification.md",
        "TASK-040-gemini-certification.md",
        "TASK-041-cline-certification.md",
        "TASK-042-opencode-certification.md",
    ):
        (checkpoint_dir / filename).write_text("# cert\n\nResult: PASS\n", encoding="utf-8")
    return checkpoint_dir


def _import_certification_evidence(
    db_path: Path,
    checkpoint_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_main(
        [
            "certify",
            "import-evidence",
            "--db-path",
            str(db_path),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--actor",
            "test_operator",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "MCPMultiplexCertificationEvidenceImport"
    assert payload["ok"] is True


def _test_file_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _check(payload: dict[str, object], name: str) -> dict[str, object]:
    checks = payload["checks"]
    assert isinstance(checks, list)
    for check in checks:
        assert isinstance(check, dict)
        if check["name"] == name:
            return check
    raise AssertionError(name)


def _insert_event_with_payload(connection: sqlite3.Connection, payload: dict[str, object]) -> None:
    migrate(connection)
    with connection:
        connection.execute(
            """
            INSERT INTO events (
              event_id,
              schema_version,
              event_type,
              actor,
              result,
              redaction,
              payload_json,
              event_hash,
              timestamp
            )
            VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt_secret_leak",
                "test.secret_leak",
                "test",
                "success",
                "secret_values_removed",
                json.dumps(payload, sort_keys=True),
                "sha256:secret-leak-test",
                "2026-06-21T00:00:00Z",
            ),
        )


def _insert_legacy_catalog_entry(connection: sqlite3.Connection, *, review_state: str) -> None:
    payload = catalog_entry().to_dict()
    payload["review_state"] = review_state
    payload["provenance"] = [
        {
            "source": "legacy_mcp_hub",
            "source_ref": "/tmp/mcp-hub-catalog.json",
            "observed_entry_id": None,
            "metadata": {"legacy_name": "context7"},
        }
    ]
    CatalogStore(connection).upsert(CatalogEntry.from_dict(payload))


def _insert_legacy_catalog_import_event(connection: sqlite3.Connection) -> None:
    EventStore(connection).append(
        event_id="evt_legacy_catalog_import_test",
        event_type="catalog.legacy_import",
        actor="test",
        result="success",
        payload={
            "source": "mcp-hub",
            "catalog_ids": ["srv_context7"],
            "source_hash": "sha256:legacy",
        },
        timestamp="2026-06-21T00:00:00Z",
    )


def _installed_unit_dir(tmp_path: Path) -> Path:
    unit_dir = tmp_path / "systemd-user"
    unit_dir.mkdir()
    (unit_dir / "mcp-multiplex.service").write_text(
        "[Unit]\nDescription=MCP Multiplex daemon\n",
        encoding="utf-8",
    )
    return unit_dir


def _fake_systemctl_bin(tmp_path: Path) -> Path:
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "printf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\n"
        "UnitFileState=enabled\\n'\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    return systemctl


def _fake_ps_bin(tmp_path: Path, output: str) -> Path:
    ps = tmp_path / "ps"
    ps.write_text(
        f"#!/bin/sh\ncat <<'EOF'\n{output}EOF\n",
        encoding="utf-8",
    )
    ps.chmod(0o755)
    return ps
