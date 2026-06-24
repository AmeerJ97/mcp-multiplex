"""Credential reference storage and readiness classification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp_multiplex.observability import EventStore, redact_secrets
from mcp_multiplex.storage import migrate

CredentialReadinessState = str

READY = "present"
MISSING = "missing"
LOCKED = "locked"
EXPIRED = "expired"
SOURCE_UNAVAILABLE = "source_unavailable"
PERMISSION_DENIED = "permission_denied"

READINESS_STATES = {
    READY,
    MISSING,
    LOCKED,
    EXPIRED,
    SOURCE_UNAVAILABLE,
    PERMISSION_DENIED,
}
SOURCE_KINDS = {"env", "dotenv", "keychain", "pass", "manual", "file", "unknown"}
SECRET_REF_PATTERN = re.compile(r"^secretref:[A-Za-z0-9_.@:/=#-]+$")


class CredentialError(ValueError):
    """Raised for invalid credential reference input."""


class CredentialResolutionError(CredentialError):
    """Raised when a secret reference cannot be resolved at an allowed boundary."""


@dataclass(frozen=True)
class CredentialRef:
    """A catalog credential reference without raw secret material."""

    credential_ref_id: str
    catalog_id: str
    name: str
    source_kind: str
    source_ref: str
    readiness_state: CredentialReadinessState = MISSING
    last_checked_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "credential_ref_id": self.credential_ref_id,
            "catalog_id": self.catalog_id,
            "name": self.name,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "readiness_state": self.readiness_state,
            "last_checked_at": self.last_checked_at,
            "metadata": redact_secrets(self.metadata),
        }


@dataclass(frozen=True)
class CredentialReadinessSummary:
    """Readiness classification for active and dormant credential references."""

    ok: bool
    blockers: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    notices: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "notices": self.notices,
        }


@dataclass(frozen=True)
class ReadinessCheck:
    """Provider readiness result that never contains a secret value."""

    readiness_state: CredentialReadinessState
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedCredentials:
    """Resolved startup environment values plus non-value resolution metadata."""

    env: dict[str, str]
    resolved_names: list[str]

    def to_event_payload(self) -> dict[str, Any]:
        """Return audit-safe metadata that excludes secret values."""
        return {"resolved_env_names": self.resolved_names, "resolved_count": len(self.env)}


class CredentialRefStore:
    """SQLite-backed credential reference repository."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate(connection)

    def upsert(self, credential: CredentialRef) -> CredentialRef:
        """Insert or update one credential reference."""
        validated = validate_credential_ref(credential)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO credential_refs (
                  credential_ref_id,
                  catalog_id,
                  name,
                  source_kind,
                  source_ref,
                  readiness_state,
                  last_checked_at,
                  metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(catalog_id, name) DO UPDATE SET
                  source_kind = excluded.source_kind,
                  source_ref = excluded.source_ref,
                  readiness_state = excluded.readiness_state,
                  last_checked_at = excluded.last_checked_at,
                  metadata_json = excluded.metadata_json
                """,
                _credential_row(validated),
            )
        return self.show(validated.credential_ref_id)

    def create(
        self,
        *,
        catalog_id: str,
        name: str,
        source_kind: str,
        source_ref: str,
        readiness_state: CredentialReadinessState = MISSING,
        last_checked_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CredentialRef:
        """Create or update a credential reference."""
        return self.upsert(
            CredentialRef(
                credential_ref_id=credential_ref_id(catalog_id, name),
                catalog_id=catalog_id,
                name=name,
                source_kind=source_kind,
                source_ref=source_ref,
                readiness_state=readiness_state,
                last_checked_at=last_checked_at,
                metadata=metadata or {},
            )
        )

    def show(self, credential_ref_id: str) -> CredentialRef:
        """Return one credential reference by id."""
        row = self.connection.execute(
            """
            SELECT *
            FROM credential_refs
            WHERE credential_ref_id = ?
            """,
            (credential_ref_id,),
        ).fetchone()
        if row is None:
            raise KeyError(credential_ref_id)
        return _credential_from_row(row)

    def list(self, *, catalog_id: str | None = None) -> list[CredentialRef]:
        """List credential references in deterministic order."""
        clauses: list[str] = []
        params: list[str] = []
        if catalog_id is not None:
            clauses.append("catalog_id = ?")
            params.append(catalog_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT credential_ref_id
            FROM credential_refs
            {where}
            ORDER BY catalog_id, name
            """,
            params,
        ).fetchall()
        return [self.show(str(row["credential_ref_id"])) for row in rows]

    def update_readiness(
        self,
        credential_ref_id: str,
        readiness_state: CredentialReadinessState,
        *,
        metadata: dict[str, Any] | None = None,
        checked_at: str | None = None,
        emit_event: bool = True,
    ) -> CredentialRef:
        """Update readiness state without resolving or storing a secret value."""
        if readiness_state not in READINESS_STATES:
            raise CredentialError(f"unsupported credential readiness state: {readiness_state}")
        current = self.show(credential_ref_id)
        timestamp = checked_at or _current_timestamp()
        redacted_metadata = redact_secrets(metadata or current.metadata)
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE credential_refs
                SET readiness_state = ?,
                    last_checked_at = ?,
                    metadata_json = ?
                WHERE credential_ref_id = ?
                """,
                (
                    readiness_state,
                    timestamp,
                    _canonical_json(redacted_metadata),
                    credential_ref_id,
                ),
            ).rowcount
        if updated == 0:
            raise KeyError(credential_ref_id)
        credential = self.show(credential_ref_id)
        if emit_event:
            _emit_credential_event(self.connection, credential)
        return credential

    def check_readiness(
        self,
        credential_ref_id: str,
        *,
        checker: CredentialReadinessChecker | None = None,
        checked_at: str | None = None,
        emit_event: bool = True,
    ) -> CredentialRef:
        """Check provider readiness without resolving secret values."""
        credential = self.show(credential_ref_id)
        readiness_checker = checker or CredentialReadinessChecker()
        result = readiness_checker.check(credential)
        return self.update_readiness(
            credential_ref_id,
            result.readiness_state,
            metadata=result.metadata,
            checked_at=checked_at,
            emit_event=emit_event,
        )

    def resolve_for_backend_startup(
        self,
        *,
        catalog_id: str,
        required_env_names: Sequence[str],
        resolver: CredentialResolver | None = None,
    ) -> ResolvedCredentials:
        """Resolve only the env names needed to start one active backend."""
        credentials = {
            credential.name: credential for credential in self.list(catalog_id=catalog_id)
        }
        resolved: dict[str, str] = {}
        missing: list[str] = []
        credential_resolver = resolver or CredentialResolver()
        for name in required_env_names:
            credential = credentials.get(name)
            if credential is None:
                missing.append(name)
                continue
            resolved[name] = credential_resolver.resolve(credential)
        if missing:
            raise CredentialResolutionError(
                "required credentials are not configured: " + ", ".join(sorted(missing))
            )
        return ResolvedCredentials(env=resolved, resolved_names=list(required_env_names))


class CredentialReadinessChecker:
    """Readiness checker for known secret reference providers."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        pass_status: Mapping[str, CredentialReadinessState] | None = None,
    ) -> None:
        self.env = env
        self.pass_status = pass_status or {}

    def check(self, credential: CredentialRef) -> ReadinessCheck:
        """Return non-value readiness metadata for one credential."""
        if credential.source_kind == "env":
            return self._check_env(credential)
        if credential.source_kind == "dotenv":
            return self._check_dotenv(credential)
        if credential.source_kind == "keychain":
            return ReadinessCheck(
                readiness_state=SOURCE_UNAVAILABLE,
                metadata={"provider": "keychain", "placeholder": True, "resolved": False},
            )
        if credential.source_kind == "pass":
            return self._check_pass_no_prompt(credential)
        return ReadinessCheck(
            readiness_state=SOURCE_UNAVAILABLE,
            metadata={"provider": credential.source_kind, "resolved": False},
        )

    def _check_env(self, credential: CredentialRef) -> ReadinessCheck:
        key = _parse_env_ref(credential)
        source = self.env if self.env is not None else {}
        return ReadinessCheck(
            readiness_state=READY if key in source else MISSING,
            metadata={"provider": "env", "name": key, "resolved": False},
        )

    def _check_dotenv(self, credential: CredentialRef) -> ReadinessCheck:
        dotenv_path, key = _parse_dotenv_ref(credential)
        if not dotenv_path.exists():
            return ReadinessCheck(
                readiness_state=SOURCE_UNAVAILABLE,
                metadata={
                    "provider": "dotenv",
                    "path": str(dotenv_path),
                    "name": key,
                    "resolved": False,
                },
            )
        try:
            names = _dotenv_names(dotenv_path)
        except OSError:
            return ReadinessCheck(
                readiness_state=PERMISSION_DENIED,
                metadata={
                    "provider": "dotenv",
                    "path": str(dotenv_path),
                    "name": key,
                    "resolved": False,
                },
            )
        return ReadinessCheck(
            readiness_state=READY if key in names else MISSING,
            metadata={
                "provider": "dotenv",
                "path": str(dotenv_path),
                "name": key,
                "resolved": False,
            },
        )

    def _check_pass_no_prompt(self, credential: CredentialRef) -> ReadinessCheck:
        pass_ref = _parse_pass_ref(credential)
        readiness_state = self.pass_status.get(pass_ref, LOCKED)
        if readiness_state not in READINESS_STATES:
            readiness_state = SOURCE_UNAVAILABLE
        return ReadinessCheck(
            readiness_state=readiness_state,
            metadata={
                "provider": "pass",
                "entry": pass_ref,
                "prompted": False,
                "resolved": False,
            },
        )


class CredentialResolver:
    """Resolve secret references only at backend startup or explicit auth flows."""

    def __init__(self, *, env_source: Mapping[str, str] | None = None) -> None:
        self.env_source = env_source if env_source is not None else os.environ

    def resolve(self, credential: CredentialRef) -> str:
        """Resolve one credential value without logging or storing it."""
        if credential.source_kind == "env":
            return self._resolve_env(credential)
        if credential.source_kind == "dotenv":
            return self._resolve_dotenv(credential)
        if credential.source_kind == "keychain":
            raise CredentialResolutionError("keychain resolution is not implemented")
        if credential.source_kind == "pass":
            raise CredentialResolutionError("pass resolution requires an explicit auth flow")
        raise CredentialResolutionError(
            f"credential source cannot be resolved for startup: {credential.source_kind}"
        )

    def _resolve_env(self, credential: CredentialRef) -> str:
        name = _parse_env_ref(credential)
        if name not in self.env_source:
            raise CredentialResolutionError(
                f"required env credential is missing: {credential.name}"
            )
        return self.env_source[name]

    def _resolve_dotenv(self, credential: CredentialRef) -> str:
        dotenv_path, key = _parse_dotenv_ref(credential)
        if not dotenv_path.exists():
            raise CredentialResolutionError(
                f"required dotenv credential source is unavailable: {credential.name}"
            )
        try:
            values = _dotenv_values(dotenv_path)
        except OSError as error:
            raise CredentialResolutionError(
                f"required dotenv credential source is unreadable: {credential.name}"
            ) from error
        if key not in values:
            raise CredentialResolutionError(
                f"required dotenv credential is missing: {credential.name}"
            )
        return values[key]


def validate_credential_ref(credential: CredentialRef) -> CredentialRef:
    """Validate credential reference shape and redaction invariants."""
    if not credential.catalog_id.startswith("srv_"):
        raise CredentialError("catalog_id must start with srv_")
    if not credential.credential_ref_id.startswith("cred_"):
        raise CredentialError("credential_ref_id must start with cred_")
    if not credential.name:
        raise CredentialError("credential name is required")
    if credential.source_kind not in SOURCE_KINDS:
        raise CredentialError(f"unsupported credential source kind: {credential.source_kind}")
    if not SECRET_REF_PATTERN.match(credential.source_ref):
        raise CredentialError("credential source_ref must be a secretref")
    if credential.readiness_state not in READINESS_STATES:
        raise CredentialError(
            f"unsupported credential readiness state: {credential.readiness_state}"
        )
    _reject_raw_secret_material(credential.metadata)
    return CredentialRef(
        credential_ref_id=credential.credential_ref_id,
        catalog_id=credential.catalog_id,
        name=credential.name,
        source_kind=credential.source_kind,
        source_ref=credential.source_ref,
        readiness_state=credential.readiness_state,
        last_checked_at=credential.last_checked_at,
        metadata=redact_secrets(credential.metadata),
    )


def credential_ref_id(catalog_id: str, name: str) -> str:
    """Return a stable credential reference id."""
    digest = hashlib.sha256(f"{catalog_id}\0{name}".encode()).hexdigest()[:24]
    return f"cred_{digest}"


def readiness_summary(
    credentials: list[CredentialRef],
    *,
    active_catalog_ids: set[str],
) -> CredentialReadinessSummary:
    """Classify credential readiness without resolving secret values."""
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    notices: list[dict[str, Any]] = []
    for credential in credentials:
        item = {
            "credential_ref_id": credential.credential_ref_id,
            "catalog_id": credential.catalog_id,
            "name": credential.name,
            "source_kind": credential.source_kind,
            "readiness_state": credential.readiness_state,
        }
        if credential.readiness_state == READY:
            notices.append(item)
            continue
        if credential.catalog_id in active_catalog_ids:
            blockers.append({**item, "code": "missing_active_credential"})
        else:
            warnings.append({**item, "code": "dormant_credential_not_ready"})
    return CredentialReadinessSummary(
        ok=not blockers,
        blockers=blockers,
        warnings=warnings,
        notices=notices,
    )


def _credential_row(
    credential: CredentialRef,
) -> tuple[str, str, str, str, str, str, str | None, str]:
    return (
        credential.credential_ref_id,
        credential.catalog_id,
        credential.name,
        credential.source_kind,
        credential.source_ref,
        credential.readiness_state,
        credential.last_checked_at,
        _canonical_json(redact_secrets(credential.metadata)),
    )


def _credential_from_row(row: sqlite3.Row) -> CredentialRef:
    return CredentialRef(
        credential_ref_id=str(row["credential_ref_id"]),
        catalog_id=str(row["catalog_id"]),
        name=str(row["name"]),
        source_kind=str(row["source_kind"]),
        source_ref=str(row["source_ref"]),
        readiness_state=str(row["readiness_state"]),
        last_checked_at=row["last_checked_at"],
        metadata=json.loads(str(row["metadata_json"])),
    )


def _emit_credential_event(connection: sqlite3.Connection, credential: CredentialRef) -> None:
    timestamp = _current_timestamp()
    digest = hashlib.sha256(
        _canonical_json(
            {
                "credential_ref_id": credential.credential_ref_id,
                "readiness_state": credential.readiness_state,
                "timestamp": timestamp,
                "nonce": uuid.uuid4().hex,
            }
        ).encode()
    ).hexdigest()[:24]
    EventStore(connection).append(
        event_id=f"evt_{digest}",
        event_type="credential.readiness_checked",
        actor="daemon",
        result="success" if credential.readiness_state == READY else "warning",
        payload={
            "credential_ref_id": credential.credential_ref_id,
            "catalog_id": credential.catalog_id,
            "name": credential.name,
            "source_kind": credential.source_kind,
            "source_ref": credential.source_ref,
            "readiness_state": credential.readiness_state,
            "metadata": credential.metadata,
        },
        timestamp=timestamp,
    )


def _reject_raw_secret_material(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _looks_like_raw_secret_key(str(key)):
                raise CredentialError("credential metadata cannot contain raw secret-like values")
            _reject_raw_secret_material(item)
    elif isinstance(value, list):
        for item in value:
            _reject_raw_secret_material(item)


def _looks_like_raw_secret_key(key: str) -> bool:
    lower = key.lower()
    return any(token in lower for token in ("secret", "token", "password", "api_key", "apikey"))


def _parse_env_ref(credential: CredentialRef) -> str:
    prefix = "secretref:env/"
    if credential.source_ref.startswith(prefix):
        return credential.source_ref.removeprefix(prefix)
    return credential.name


def _parse_dotenv_ref(credential: CredentialRef) -> tuple[Path, str]:
    prefix = "secretref:dotenv:"
    if not credential.source_ref.startswith(prefix) or "#" not in credential.source_ref:
        raise CredentialError("dotenv source_ref must be secretref:dotenv:/path/to/.env#NAME")
    location = credential.source_ref.removeprefix(prefix)
    path_text, name = location.rsplit("#", 1)
    if not path_text or not name:
        raise CredentialError("dotenv source_ref must include path and variable name")
    return Path(path_text), name


def _parse_pass_ref(credential: CredentialRef) -> str:
    prefix = "secretref:pass/"
    if credential.source_ref.startswith(prefix):
        return credential.source_ref.removeprefix(prefix)
    return credential.source_ref.removeprefix("secretref:")


def _dotenv_names(path: Path) -> set[str]:
    names: set[str] = set()
    for name, _value in _iter_dotenv_assignments(path):
        names.add(name)
    return names


def _dotenv_values(path: Path) -> dict[str, str]:
    return dict(_iter_dotenv_assignments(path))


def _iter_dotenv_assignments(path: Path) -> list[tuple[str, str]]:
    assignments: list[tuple[str, str]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name.removeprefix("export ").strip()
        if not name:
            continue
        assignments.append((name, _strip_dotenv_value(value.strip())))
    return assignments


def _strip_dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _current_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "CredentialError",
    "CredentialReadinessState",
    "CredentialReadinessChecker",
    "CredentialReadinessSummary",
    "CredentialRef",
    "CredentialRefStore",
    "CredentialResolutionError",
    "CredentialResolver",
    "EXPIRED",
    "LOCKED",
    "MISSING",
    "PERMISSION_DENIED",
    "READY",
    "READINESS_STATES",
    "ReadinessCheck",
    "ResolvedCredentials",
    "SOURCE_KINDS",
    "SOURCE_UNAVAILABLE",
    "credential_ref_id",
    "readiness_summary",
    "validate_credential_ref",
]
