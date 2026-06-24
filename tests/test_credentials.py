from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mcp_multiplex.credentials import (
    LOCKED,
    MISSING,
    READY,
    SOURCE_UNAVAILABLE,
    CredentialError,
    CredentialReadinessChecker,
    CredentialRefStore,
    CredentialResolutionError,
    CredentialResolver,
    credential_ref_id,
    readiness_summary,
)
from mcp_multiplex.observability import REDACTED_VALUE, EventStore
from mcp_multiplex.storage import connect, migrate
from tests.test_schema_models import catalog_entry_payload


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "multiplex.db")
    migrate(connection)
    return connection


def test_credential_reference_store_persists_secret_refs_only(
    connection: sqlite3.Connection,
) -> None:
    insert_catalog(connection, "srv_context7")
    credential = CredentialRefStore(connection).create(
        catalog_id="srv_context7",
        name="GITHUB_TOKEN",
        source_kind="env",
        source_ref="secretref:env/GITHUB_TOKEN",
        readiness_state=MISSING,
        metadata={"description": "GitHub API access"},
    )

    assert credential.credential_ref_id == credential_ref_id("srv_context7", "GITHUB_TOKEN")
    assert credential.to_dict() == {
        "credential_ref_id": credential.credential_ref_id,
        "catalog_id": "srv_context7",
        "name": "GITHUB_TOKEN",
        "source_kind": "env",
        "source_ref": "secretref:env/GITHUB_TOKEN",
        "readiness_state": "missing",
        "last_checked_at": None,
        "metadata": {"description": "GitHub API access"},
    }

    stored = connection.execute(
        "SELECT source_ref, metadata_json FROM credential_refs WHERE credential_ref_id = ?",
        (credential.credential_ref_id,),
    ).fetchone()
    assert stored["source_ref"] == "secretref:env/GITHUB_TOKEN"
    assert "raw-token" not in str(stored["metadata_json"])


def test_credential_reference_rejects_raw_source_values(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(CredentialError, match="source_ref must be a secretref"):
        CredentialRefStore(connection).create(
            catalog_id="srv_context7",
            name="GITHUB_TOKEN",
            source_kind="env",
            source_ref="ghp_raw_secret_value",
        )


def test_credential_metadata_rejects_secret_like_keys(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(CredentialError, match="raw secret-like values"):
        CredentialRefStore(connection).create(
            catalog_id="srv_context7",
            name="GITHUB_TOKEN",
            source_kind="env",
            source_ref="secretref:env/GITHUB_TOKEN",
            metadata={"token": "raw-token"},
        )


def test_update_readiness_records_redacted_event_without_secret_resolution(
    connection: sqlite3.Connection,
) -> None:
    insert_catalog(connection, "srv_context7")
    store = CredentialRefStore(connection)
    credential = store.create(
        catalog_id="srv_context7",
        name="PASS_TOKEN",
        source_kind="pass",
        source_ref="secretref:pass/context7/token",
        readiness_state=MISSING,
    )

    updated = store.update_readiness(
        credential.credential_ref_id,
        LOCKED,
        metadata={"provider": "pass", "prompted": False},
        checked_at="2026-06-20T00:00:00Z",
    )

    assert updated.readiness_state == LOCKED
    assert updated.metadata == {"provider": "pass", "prompted": False}
    events = EventStore(connection).query(event_type="credential.readiness_checked")
    assert len(events) == 1
    assert events[0].payload == {
        "catalog_id": "srv_context7",
        "credential_ref_id": REDACTED_VALUE,
        "metadata": {"provider": "pass", "prompted": False},
        "name": "PASS_TOKEN",
        "readiness_state": "locked",
        "source_kind": "pass",
        "source_ref": "secretref:pass/context7/token",
    }
    event_row = connection.execute(
        "SELECT payload_json FROM events WHERE event_id = ?",
        (events[0].event.event_id,),
    ).fetchone()
    payload_json = str(event_row["payload_json"])
    assert "raw-token" not in payload_json
    assert "secretref:pass/context7/token" in payload_json


def test_readiness_summary_blocks_active_and_warns_dormant(
    connection: sqlite3.Connection,
) -> None:
    insert_catalog(connection, "srv_active")
    insert_catalog(connection, "srv_dormant")
    insert_catalog(connection, "srv_present")
    store = CredentialRefStore(connection)
    active = store.create(
        catalog_id="srv_active",
        name="ACTIVE_TOKEN",
        source_kind="env",
        source_ref="secretref:env/ACTIVE_TOKEN",
        readiness_state=MISSING,
    )
    dormant = store.create(
        catalog_id="srv_dormant",
        name="DORMANT_TOKEN",
        source_kind="env",
        source_ref="secretref:env/DORMANT_TOKEN",
        readiness_state=MISSING,
    )
    present = store.create(
        catalog_id="srv_present",
        name="PRESENT_TOKEN",
        source_kind="env",
        source_ref="secretref:env/PRESENT_TOKEN",
        readiness_state=READY,
    )

    summary = readiness_summary(
        [active, dormant, present],
        active_catalog_ids={"srv_active"},
    )

    assert summary.ok is False
    assert summary.blockers == [
        {
            "catalog_id": "srv_active",
            "code": "missing_active_credential",
            "credential_ref_id": active.credential_ref_id,
            "name": "ACTIVE_TOKEN",
            "readiness_state": "missing",
            "source_kind": "env",
        }
    ]
    assert summary.warnings == [
        {
            "catalog_id": "srv_dormant",
            "code": "dormant_credential_not_ready",
            "credential_ref_id": dormant.credential_ref_id,
            "name": "DORMANT_TOKEN",
            "readiness_state": "missing",
            "source_kind": "env",
        }
    ]
    assert summary.notices == [
        {
            "catalog_id": "srv_present",
            "credential_ref_id": present.credential_ref_id,
            "name": "PRESENT_TOKEN",
            "readiness_state": "present",
            "source_kind": "env",
        }
    ]
    assert json.dumps(summary.to_dict(), sort_keys=True)


def test_env_readiness_checks_presence_without_value(
    connection: sqlite3.Connection,
) -> None:
    insert_catalog(connection, "srv_context7")
    store = CredentialRefStore(connection)
    credential = store.create(
        catalog_id="srv_context7",
        name="SERVICE_TOKEN",
        source_kind="env",
        source_ref="secretref:env/SERVICE_TOKEN",
    )

    checked = store.check_readiness(
        credential.credential_ref_id,
        checker=CredentialReadinessChecker(env={"SERVICE_TOKEN": "fixture-value"}),
        checked_at="2026-06-20T00:00:00Z",
    )

    assert checked.readiness_state == READY
    assert checked.metadata == {"provider": "env", "name": "SERVICE_TOKEN", "resolved": False}
    stored = connection.execute(
        "SELECT metadata_json FROM credential_refs WHERE credential_ref_id = ?",
        (credential.credential_ref_id,),
    ).fetchone()
    assert "fixture-value" not in str(stored["metadata_json"])


def test_dotenv_readiness_reads_names_without_storing_values(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    insert_catalog(connection, "srv_context7")
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("SERVICE_TOKEN=\nOTHER_NAME=\n", encoding="utf-8")
    store = CredentialRefStore(connection)
    credential = store.create(
        catalog_id="srv_context7",
        name="SERVICE_TOKEN",
        source_kind="dotenv",
        source_ref=f"secretref:dotenv:{dotenv_path}#SERVICE_TOKEN",
    )

    checked = store.check_readiness(
        credential.credential_ref_id,
        checker=CredentialReadinessChecker(),
        checked_at="2026-06-20T00:00:00Z",
    )

    assert checked.readiness_state == READY
    assert checked.metadata == {
        "provider": "dotenv",
        "path": str(dotenv_path),
        "name": "SERVICE_TOKEN",
        "resolved": False,
    }
    stored = connection.execute(
        "SELECT metadata_json FROM credential_refs WHERE credential_ref_id = ?",
        (credential.credential_ref_id,),
    ).fetchone()
    assert "OTHER_NAME" not in str(stored["metadata_json"])


def test_keychain_readiness_placeholder_is_source_unavailable(
    connection: sqlite3.Connection,
) -> None:
    insert_catalog(connection, "srv_context7")
    store = CredentialRefStore(connection)
    credential = store.create(
        catalog_id="srv_context7",
        name="KEYCHAIN_TOKEN",
        source_kind="keychain",
        source_ref="secretref:keychain/login-item",
    )

    checked = store.check_readiness(
        credential.credential_ref_id,
        checker=CredentialReadinessChecker(),
        checked_at="2026-06-20T00:00:00Z",
    )

    assert checked.readiness_state == SOURCE_UNAVAILABLE
    assert checked.metadata == {
        "provider": "keychain",
        "placeholder": True,
        "resolved": False,
    }


def test_pass_readiness_uses_metadata_without_prompting(
    connection: sqlite3.Connection,
) -> None:
    insert_catalog(connection, "srv_context7")
    store = CredentialRefStore(connection)
    credential = store.create(
        catalog_id="srv_context7",
        name="PASS_TOKEN",
        source_kind="pass",
        source_ref="secretref:pass/context7/token",
    )

    checked = store.check_readiness(
        credential.credential_ref_id,
        checker=CredentialReadinessChecker(),
        checked_at="2026-06-20T00:00:00Z",
    )

    assert checked.readiness_state == LOCKED
    assert checked.metadata == {
        "provider": "pass",
        "entry": "context7/token",
        "prompted": False,
        "resolved": False,
    }


def test_backend_startup_resolves_only_required_env_names(
    connection: sqlite3.Connection,
) -> None:
    insert_catalog(connection, "srv_context7")
    store = CredentialRefStore(connection)
    store.create(
        catalog_id="srv_context7",
        name="SERVICE_TOKEN",
        source_kind="env",
        source_ref="secretref:env/SERVICE_TOKEN",
    )
    store.create(
        catalog_id="srv_context7",
        name="DORMANT_TOKEN",
        source_kind="env",
        source_ref="secretref:env/DORMANT_TOKEN",
    )

    resolved = store.resolve_for_backend_startup(
        catalog_id="srv_context7",
        required_env_names=["SERVICE_TOKEN"],
        resolver=CredentialResolver(
            env_source={
                "SERVICE_TOKEN": "runtime-fixture-value",
                "DORMANT_TOKEN": "unused-fixture-value",
            }
        ),
    )

    assert resolved.env == {"SERVICE_TOKEN": "runtime-fixture-value"}
    assert resolved.to_event_payload() == {
        "resolved_env_names": ["SERVICE_TOKEN"],
        "resolved_count": 1,
    }


def test_backend_startup_resolves_dotenv_value_without_persisting_it(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    insert_catalog(connection, "srv_context7")
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text('SERVICE_TOKEN="runtime-fixture-value"\n', encoding="utf-8")
    store = CredentialRefStore(connection)
    store.create(
        catalog_id="srv_context7",
        name="SERVICE_TOKEN",
        source_kind="dotenv",
        source_ref=f"secretref:dotenv:{dotenv_path}#SERVICE_TOKEN",
    )

    resolved = store.resolve_for_backend_startup(
        catalog_id="srv_context7",
        required_env_names=["SERVICE_TOKEN"],
        resolver=CredentialResolver(env_source={}),
    )

    assert resolved.env == {"SERVICE_TOKEN": "runtime-fixture-value"}
    stored = connection.execute(
        "SELECT metadata_json FROM credential_refs WHERE catalog_id = ?",
        ("srv_context7",),
    ).fetchone()
    assert "runtime-fixture-value" not in str(stored["metadata_json"])


def test_backend_startup_blocks_missing_required_env_value(
    connection: sqlite3.Connection,
) -> None:
    insert_catalog(connection, "srv_context7")
    store = CredentialRefStore(connection)
    store.create(
        catalog_id="srv_context7",
        name="SERVICE_TOKEN",
        source_kind="env",
        source_ref="secretref:env/SERVICE_TOKEN",
    )

    with pytest.raises(CredentialResolutionError, match="required env credential is missing"):
        store.resolve_for_backend_startup(
            catalog_id="srv_context7",
            required_env_names=["SERVICE_TOKEN"],
            resolver=CredentialResolver(env_source={}),
        )


def insert_catalog(connection: sqlite3.Connection, catalog_id: str) -> None:
    payload = catalog_entry_payload()
    name = catalog_id.removeprefix("srv_").replace("_", "-")
    payload["catalog_id"] = catalog_id
    payload["name"] = name
    payload["canonical_name"] = f"test.{name}"
    payload["family_id"] = name
    payload["variant_name"] = "credential_fixture"
    payload["display_label"] = name
    payload["aliases"] = [name]
    transport = payload["transport"]
    assert isinstance(transport, dict)
    transport["hub_path"] = f"/servers/{name}/mcp"
    connection.execute(
        """
        INSERT INTO catalog_entries (
          catalog_id,
          schema_version,
          name,
          canonical_name,
          family_id,
          variant_name,
          display_label,
          review_state,
          lifecycle_state,
          risk_tier,
          transport_json,
          runtime_json,
          credentials_json,
          active_set_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["catalog_id"],
            payload["schema_version"],
            payload["name"],
            payload["canonical_name"],
            payload["family_id"],
            payload["variant_name"],
            payload["display_label"],
            payload["review_state"],
            payload["lifecycle_state"],
            payload["risk_tier"],
            json.dumps(payload["transport"], sort_keys=True),
            json.dumps(payload["runtime"], sort_keys=True),
            json.dumps(payload["credentials"], sort_keys=True),
            json.dumps(payload["active_set"], sort_keys=True),
        ),
    )
    connection.commit()
