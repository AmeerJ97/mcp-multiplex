from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_multiplex.catalog import CatalogStore, plan_legacy_mcp_hub_catalog_import
from mcp_multiplex.cli import main as cli_main
from mcp_multiplex.observability import EventStore
from mcp_multiplex.storage import connect


def test_legacy_catalog_import_dry_run_normalizes_without_mutating_db(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog_path = _legacy_catalog_path(tmp_path)
    db_path = tmp_path / "multiplex.db"

    assert (
        cli_main(
            [
                "cutover",
                "import-catalog",
                "--from",
                "mcp-hub",
                "--catalog-path",
                str(catalog_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    result = payload["result"]
    entry = result["entries"][0]
    assert payload["kind"] == "MCPMultiplexLegacyCatalogImport"
    assert payload["mode"] == "dry_run"
    assert result["applied"] is False
    assert result["entry_count"] == 1
    assert result["warnings"][0]["code"] == "legacy_env_values_redacted"
    assert entry["catalog_id"] == "srv_context7"
    assert entry["review_state"] == "pending"
    assert entry["transport"]["hub_path"] == "/servers/context7/mcp"
    assert entry["transport"]["backend"]["command"] == "npx"
    assert entry["transport"]["backend"]["env"] == ["CONTEXT7_TOKEN"]
    assert not db_path.exists()


def test_legacy_catalog_import_apply_persists_entries_and_audit_event(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog_path = _legacy_catalog_path(tmp_path)
    db_path = tmp_path / "multiplex.db"

    assert (
        cli_main(
            [
                "cutover",
                "import-catalog",
                "--from",
                "mcp-hub",
                "--catalog-path",
                str(catalog_path),
                "--db-path",
                str(db_path),
                "--apply",
                "--actor",
                "test_operator",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    result = payload["result"]
    assert payload["mode"] == "apply"
    assert result["applied"] is True

    connection = connect(db_path)
    stored = CatalogStore(connection).show("srv_context7")
    assert stored.name == "context7"
    assert stored.review_state == "pending"
    assert stored.transport.backend.env == ["CONTEXT7_TOKEN"]
    assert stored.provenance[0]["source"] == "legacy_mcp_hub"

    events = EventStore(connection).query(event_type="catalog.legacy_import")
    assert len(events) == 1
    assert events[0].event.actor == "test_operator"
    assert events[0].payload["catalog_ids"] == ["srv_context7"]
    assert events[0].payload["warnings"][0]["code"] == "legacy_env_values_redacted"


def test_legacy_catalog_review_bulk_dry_run_and_apply_are_audited(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog_path = _legacy_catalog_path(tmp_path)
    db_path = tmp_path / "multiplex.db"

    assert (
        cli_main(
            [
                "cutover",
                "import-catalog",
                "--from",
                "mcp-hub",
                "--catalog-path",
                str(catalog_path),
                "--db-path",
                str(db_path),
                "--apply",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        cli_main(
            [
                "catalog",
                "review-legacy-import",
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    dry_run_payload = json.loads(capsys.readouterr().out)
    assert dry_run_payload["kind"] == "MCPMultiplexLegacyCatalogReview"
    assert dry_run_payload["mode"] == "dry_run"
    assert dry_run_payload["entry_count"] == 1
    assert dry_run_payload["changed_count"] == 1
    assert CatalogStore(connect(db_path)).show("srv_context7").review_state == "pending"

    assert (
        cli_main(
            [
                "catalog",
                "review-legacy-import",
                "--db-path",
                str(db_path),
                "--apply",
                "--actor",
                "test_operator",
                "--comment",
                "legacy catalog reviewed for cutover",
            ]
        )
        == 0
    )

    apply_payload = json.loads(capsys.readouterr().out)
    assert apply_payload["mode"] == "apply"
    assert apply_payload["changed_count"] == 1
    stored = CatalogStore(connect(db_path)).show("srv_context7")
    assert stored.review_state == "approved"
    assert stored.lifecycle_state == "enabled"
    events = EventStore(connect(db_path)).query(event_type="catalog.reviewed")
    assert len(events) == 1
    assert events[0].event.actor == "test_operator"
    assert events[0].payload["review_scope"] == "legacy_mcp_hub"
    assert events[0].payload["comment"] == "legacy catalog reviewed for cutover"


def test_legacy_catalog_import_rejects_raw_credential_values(tmp_path: Path) -> None:
    catalog_path = tmp_path / "legacy-catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "unsafe",
                        "command": "uvx",
                        "args": ["unsafe-mcp"],
                        "credentials": [{"name": "TOKEN", "value": "do-not-store"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    plan = plan_legacy_mcp_hub_catalog_import(catalog_path)

    assert plan.ok is False
    assert plan.errors[0]["entry"] == "unsafe"
    assert "raw value" in plan.errors[0]["detail"]


def test_legacy_catalog_import_splits_safe_stdio_command_string(tmp_path: Path) -> None:
    catalog_path = tmp_path / "legacy-catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "servers": {
                    "context7": {
                        "command": "npx -y @upstash/context7-mcp",
                        "env": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    plan = plan_legacy_mcp_hub_catalog_import(catalog_path)

    assert plan.ok is True
    entry = plan.entries[0]
    assert entry.transport.backend.command == "npx"
    assert entry.transport.backend.args == ["-y", "@upstash/context7-mcp"]
    assert plan.warnings[0]["code"] == "legacy_stdio_command_split"


def test_legacy_catalog_import_rejects_unsafe_stdio_shell_string(tmp_path: Path) -> None:
    catalog_path = tmp_path / "legacy-catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "servers": {
                    "unsafe": {
                        "command": "TOKEN=$TOKEN npx unsafe-mcp",
                        "env": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    plan = plan_legacy_mcp_hub_catalog_import(catalog_path)

    assert plan.ok is False
    assert "unsupported shell syntax" in plan.errors[0]["detail"]


def test_legacy_catalog_import_derives_local_http_url_from_port(tmp_path: Path) -> None:
    catalog_path = tmp_path / "legacy-catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "servers": {
                    "local-http": {
                        "transport": "streamable-http",
                        "port": 30123,
                        "command": "npx -y local-http-mcp",
                        "required_env": ["LOCAL_HTTP_TOKEN"],
                        "env": {"LOCAL_HTTP_TOKEN": "${LOCAL_HTTP_TOKEN:-}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    plan = plan_legacy_mcp_hub_catalog_import(catalog_path)

    assert plan.ok is True
    entry = plan.entries[0]
    assert entry.transport.backend.type == "streamable_http"
    assert entry.transport.backend.url == "http://127.0.0.1:30123/mcp"
    assert entry.transport.backend.command is None
    assert entry.transport.backend.env == ["LOCAL_HTTP_TOKEN"]
    assert {warning["code"] for warning in plan.warnings} == {
        "legacy_http_start_command_not_imported",
        "legacy_env_values_redacted",
    }


def _legacy_catalog_path(tmp_path: Path) -> Path:
    catalog_path = tmp_path / "legacy-catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "context7",
                        "canonical_name": "upstash.context7",
                        "variant_name": "legacy_npm",
                        "display_label": "Context7",
                        "aliases": ["context7-mcp"],
                        "package": "@upstash/context7-mcp",
                        "command": "npx",
                        "args": ["-y", "@upstash/context7-mcp"],
                        "env": {"CONTEXT7_TOKEN": "redacted-by-importer"},
                        "credentials": [
                            {
                                "name": "CONTEXT7_TOKEN",
                                "source_kind": "env",
                                "source_ref": "secretref:env/CONTEXT7_TOKEN",
                            }
                        ],
                    }
                ]
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return catalog_path
