"""Local control-plane and agent registration token management."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mcp_multiplex.storage import migrate

CONTROL_READ = "control:read"
CONTROL_MUTATE = "control:mutate"
AGENT_REGISTER = "agent:register"
RUNTIME_ADMIN = "runtime:admin"

AUTH_SCOPES = frozenset({CONTROL_READ, CONTROL_MUTATE, AGENT_REGISTER, RUNTIME_ADMIN})
SUBJECT_TYPES = frozenset({"operator", "agent", "daemon"})


class AuthError(ValueError):
    """Raised for invalid local authentication input or denied credentials."""


@dataclass(frozen=True)
class IssuedToken:
    """Token returned exactly once to the caller that requested issuance."""

    token_id: str
    token: str
    token_ref: str
    subject_type: str
    subject_id: str | None
    scopes: list[str]
    expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "token": self.token,
            "token_ref": self.token_ref,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "scopes": self.scopes,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class AuthTokenRecord:
    """Stored auth token metadata without raw token material."""

    token_id: str
    subject_type: str
    subject_id: str | None
    scopes: list[str]
    token_ref: str
    created_at: str
    expires_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None and not _is_expired(self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "scopes": self.scopes,
            "token_ref": self.token_ref,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_used_at": self.last_used_at,
            "revoked_at": self.revoked_at,
            "active": self.active,
        }


@dataclass(frozen=True)
class RegistrationTokenRecord:
    """Stored agent registration token metadata without raw token material."""

    registration_token_id: str
    agent_id: str
    agent_kind: str
    scopes: list[str]
    token_ref: str
    created_at: str
    expires_at: str | None = None
    consumed_at: str | None = None
    revoked_at: str | None = None

    @property
    def active(self) -> bool:
        return (
            self.consumed_at is None
            and self.revoked_at is None
            and not _is_expired(self.expires_at)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "registration_token_id": self.registration_token_id,
            "agent_id": self.agent_id,
            "agent_kind": self.agent_kind,
            "scopes": self.scopes,
            "token_ref": self.token_ref,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "consumed_at": self.consumed_at,
            "revoked_at": self.revoked_at,
            "active": self.active,
        }


@dataclass(frozen=True)
class AuthContext:
    """Verified local-auth subject and scopes."""

    subject_type: str
    subject_id: str | None
    scopes: set[str] = field(default_factory=set)
    token_ref: str = ""

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AuthError(f"token is missing required scope: {scope}")


class AuthTokenStore:
    """SQLite-backed local auth and agent registration token store."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate(connection)

    def issue_local_token(
        self,
        *,
        subject_type: str,
        subject_id: str | None = None,
        scopes: list[str],
        expires_at: str | None = None,
        token: str | None = None,
    ) -> IssuedToken:
        """Issue a local control-plane token and persist only its hash."""
        _validate_subject(subject_type)
        normalized_scopes = _validate_scopes(scopes)
        raw_token = token or _new_raw_token("mcpgt")
        token_id = _token_id("auth", raw_token)
        token_ref = f"secretref:auth/{token_id}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO auth_tokens (
                  token_id,
                  token_hash,
                  subject_type,
                  subject_id,
                  scopes_json,
                  token_ref,
                  expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    _hash_token(raw_token),
                    subject_type,
                    subject_id,
                    _canonical_json(normalized_scopes),
                    token_ref,
                    expires_at,
                ),
            )
        record = self.show_local_token(token_id)
        _emit_auth_event(
            self.connection,
            "auth.token_issued",
            "success",
            {
                "token_ref": record.token_ref,
                "subject_type": record.subject_type,
                "subject_id": record.subject_id,
                "scopes": record.scopes,
            },
        )
        return IssuedToken(
            token_id=record.token_id,
            token=raw_token,
            token_ref=record.token_ref,
            subject_type=record.subject_type,
            subject_id=record.subject_id,
            scopes=record.scopes,
            expires_at=record.expires_at,
        )

    def issue_agent_registration_token(
        self,
        *,
        agent_id: str,
        agent_kind: str,
        scopes: list[str] | None = None,
        expires_at: str | None = None,
        token: str | None = None,
    ) -> IssuedToken:
        """Issue a one-time token that an installed mcp_hub entry can exchange."""
        if not agent_id:
            raise AuthError("agent_id is required")
        if not agent_kind:
            raise AuthError("agent_kind is required")
        normalized_scopes = _validate_scopes(scopes or [CONTROL_READ])
        raw_token = token or _new_raw_token("mcpgr")
        registration_token_id = _token_id("reg", raw_token)
        token_ref = f"secretref:agent-registration/{registration_token_id}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO agent_registration_tokens (
                  registration_token_id,
                  token_hash,
                  agent_id,
                  agent_kind,
                  scopes_json,
                  token_ref,
                  expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    registration_token_id,
                    _hash_token(raw_token),
                    agent_id,
                    agent_kind,
                    _canonical_json(normalized_scopes),
                    token_ref,
                    expires_at,
                ),
            )
        record = self.show_agent_registration_token(registration_token_id)
        _emit_auth_event(
            self.connection,
            "auth.agent_registration_token_issued",
            "success",
            {
                "token_ref": record.token_ref,
                "agent_id": record.agent_id,
                "agent_kind": record.agent_kind,
                "scopes": record.scopes,
            },
        )
        return IssuedToken(
            token_id=record.registration_token_id,
            token=raw_token,
            token_ref=record.token_ref,
            subject_type="agent",
            subject_id=record.agent_id,
            scopes=record.scopes,
            expires_at=record.expires_at,
        )

    def exchange_agent_registration_token(self, token: str) -> IssuedToken:
        """Consume a registration token and return a durable agent auth token."""
        registration = self.verify_agent_registration_token(token)
        timestamp = _current_timestamp()
        with self.connection:
            self.connection.execute(
                """
                UPDATE agent_registration_tokens
                SET consumed_at = ?
                WHERE registration_token_id = ?
                """,
                (timestamp, registration.registration_token_id),
            )
        issued = self.issue_local_token(
            subject_type="agent",
            subject_id=registration.agent_id,
            scopes=registration.scopes,
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE agents
                SET auth_token_ref = ?, last_seen_at = ?
                WHERE agent_id = ?
                """,
                (issued.token_ref, timestamp, registration.agent_id),
            )
        _emit_auth_event(
            self.connection,
            "auth.agent_registered",
            "success",
            {
                "agent_id": registration.agent_id,
                "agent_kind": registration.agent_kind,
                "auth_token_ref": issued.token_ref,
                "registration_token_ref": registration.token_ref,
                "scopes": registration.scopes,
            },
            agent_id=registration.agent_id,
        )
        return issued

    def verify_local_token(
        self,
        token: str,
        *,
        required_scope: str | None = None,
    ) -> AuthContext:
        """Verify one local token and optionally enforce a scope."""
        row = self.connection.execute(
            """
            SELECT *
            FROM auth_tokens
            WHERE token_hash = ?
            """,
            (_hash_token(token),),
        ).fetchone()
        if row is None:
            raise AuthError("invalid auth token")
        record = _auth_token_from_row(row)
        if not record.active:
            raise AuthError("auth token is not active")
        context = AuthContext(
            subject_type=record.subject_type,
            subject_id=record.subject_id,
            scopes=set(record.scopes),
            token_ref=record.token_ref,
        )
        if required_scope is not None:
            context.require_scope(required_scope)
        with self.connection:
            self.connection.execute(
                """
                UPDATE auth_tokens
                SET last_used_at = ?
                WHERE token_id = ?
                """,
                (_current_timestamp(), record.token_id),
            )
        return context

    def verify_agent_registration_token(self, token: str) -> RegistrationTokenRecord:
        """Verify a one-time agent registration token without consuming it."""
        row = self.connection.execute(
            """
            SELECT *
            FROM agent_registration_tokens
            WHERE token_hash = ?
            """,
            (_hash_token(token),),
        ).fetchone()
        if row is None:
            raise AuthError("invalid agent registration token")
        record = _registration_token_from_row(row)
        if not record.active:
            raise AuthError("agent registration token is not active")
        return record

    def revoke_local_token(
        self, token_id: str, *, revoked_at: str | None = None
    ) -> AuthTokenRecord:
        """Revoke one local token by id."""
        timestamp = revoked_at or _current_timestamp()
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE auth_tokens
                SET revoked_at = ?
                WHERE token_id = ?
                """,
                (timestamp, token_id),
            ).rowcount
        if updated == 0:
            raise KeyError(token_id)
        return self.show_local_token(token_id)

    def show_local_token(self, token_id: str) -> AuthTokenRecord:
        row = self.connection.execute(
            """
            SELECT *
            FROM auth_tokens
            WHERE token_id = ?
            """,
            (token_id,),
        ).fetchone()
        if row is None:
            raise KeyError(token_id)
        return _auth_token_from_row(row)

    def show_agent_registration_token(self, registration_token_id: str) -> RegistrationTokenRecord:
        row = self.connection.execute(
            """
            SELECT *
            FROM agent_registration_tokens
            WHERE registration_token_id = ?
            """,
            (registration_token_id,),
        ).fetchone()
        if row is None:
            raise KeyError(registration_token_id)
        return _registration_token_from_row(row)


def _auth_token_from_row(row: sqlite3.Row) -> AuthTokenRecord:
    return AuthTokenRecord(
        token_id=str(row["token_id"]),
        subject_type=str(row["subject_type"]),
        subject_id=row["subject_id"],
        scopes=json.loads(str(row["scopes_json"])),
        token_ref=str(row["token_ref"]),
        created_at=str(row["created_at"]),
        expires_at=row["expires_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )


def _registration_token_from_row(row: sqlite3.Row) -> RegistrationTokenRecord:
    return RegistrationTokenRecord(
        registration_token_id=str(row["registration_token_id"]),
        agent_id=str(row["agent_id"]),
        agent_kind=str(row["agent_kind"]),
        scopes=json.loads(str(row["scopes_json"])),
        token_ref=str(row["token_ref"]),
        created_at=str(row["created_at"]),
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        revoked_at=row["revoked_at"],
    )


def _validate_subject(subject_type: str) -> None:
    if subject_type not in SUBJECT_TYPES:
        raise AuthError(f"unsupported subject_type: {subject_type}")


def _validate_scopes(scopes: list[str]) -> list[str]:
    if not scopes:
        raise AuthError("at least one scope is required")
    unknown = sorted(set(scopes) - AUTH_SCOPES)
    if unknown:
        raise AuthError(f"unsupported auth scopes: {unknown}")
    return sorted(set(scopes))


def _hash_token(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _token_id(prefix: str, token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _new_raw_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _is_expired(expires_at: str | None) -> bool:
    if expires_at is None:
        return False
    return _parse_timestamp(expires_at) <= datetime.now(UTC)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _emit_auth_event(
    connection: sqlite3.Connection,
    event_type: str,
    result: str,
    payload: dict[str, Any],
    *,
    agent_id: str | None = None,
) -> None:
    from mcp_multiplex.observability.audit import EventStore

    timestamp = _current_timestamp()
    digest = hashlib.sha256(
        _canonical_json(
            {
                "event_type": event_type,
                "payload": payload,
                "timestamp": timestamp,
                "nonce": uuid.uuid4().hex,
            }
        ).encode()
    ).hexdigest()[:24]
    EventStore(connection).append(
        event_id=f"evt_{digest}",
        event_type=event_type,
        actor="daemon",
        agent_id=agent_id,
        result=result,
        payload=payload,
        timestamp=timestamp,
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _current_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "AGENT_REGISTER",
    "AUTH_SCOPES",
    "CONTROL_MUTATE",
    "CONTROL_READ",
    "RUNTIME_ADMIN",
    "AuthContext",
    "AuthError",
    "AuthTokenRecord",
    "AuthTokenStore",
    "IssuedToken",
    "RegistrationTokenRecord",
]
