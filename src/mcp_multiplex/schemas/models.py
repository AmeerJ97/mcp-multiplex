"""Dataclass-backed schema models for MCP Multiplex contracts."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Literal, TypeVar, get_args

from mcp_multiplex.security import SecurityError, validate_command_name, validate_http_url

JsonObject = dict[str, Any]
T = TypeVar("T", bound="SchemaModel")


class ValidationError(ValueError):
    """Raised when schema input is incomplete or unsafe."""


AGENT_KINDS = {"codex", "claude_code", "gemini", "cline", "opencode"}
TRANSPORTS = {"stdio", "streamable_http", "http", "sse"}
PARSER_CONFIDENCE = {"complete", "partial", "opaque"}
CATALOG_REVIEW_STATES = {"approved", "pending", "rejected", "quarantined"}
CATALOG_LIFECYCLE_STATES = {"enabled", "disabled", "deprecated"}
RISK_TIERS = {"low", "normal", "high", "unknown"}
SHAREABILITY = {
    "global",
    "per_workspace",
    "per_agent",
    "per_account",
    "isolated_per_frontend_session",
    "no_proxy",
}
CONCURRENCY = {"concurrent_readonly", "serialized", "exclusive"}
CANDIDATE_CLASSIFICATIONS = {"unknown_stdio", "unknown_local_http", "unknown_remote_http"}
CANDIDATE_CONFIDENCE = {"low", "medium", "high"}
PLAN_TYPES = {
    "rewrite_known_direct",
    "import_unknown_candidate",
    "route_approved_candidate",
    "install_missing_control_plane",
    "remove_duplicate_bypass",
    "profile_extra_detected",
    "unsafe_local_http_detected",
    "unsupported_config_detected",
}
PLAN_STATUSES = {"draft", "pending_approval", "approved", "rejected", "applied", "failed"}
APPROVAL_STATES = {
    "not_required",
    "pending",
    "approved",
    "rejected",
    "expired",
    "applied",
    "revoked",
}
RUNTIME_BACKEND_STATES = {"cold", "starting", "hot", "idle", "crashed", "stopped"}
HEALTH_AREAS = {"daemon", "compliance", "runtime", "credentials", "storage", "security"}


def _require_string(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{key} is required")
    return value


def _optional_string(payload: JsonObject, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{key} must be a string or null")
    return value


def _require_bool(payload: JsonObject, key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValidationError(f"{key} must be a boolean")
    return value


def _require_int(payload: JsonObject, key: str, *, minimum: int | None = None) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{key} must be >= {minimum}")
    return value


def _require_list(payload: JsonObject, key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValidationError(f"{key} must be a list")
    return value


def _require_string_list(payload: JsonObject, key: str) -> list[str]:
    values = _require_list(payload, key)
    if not all(isinstance(value, str) for value in values):
        raise ValidationError(f"{key} must contain only strings")
    return values


def _require_object(payload: JsonObject, key: str) -> JsonObject:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValidationError(f"{key} must be an object")
    return dict(value)


def _optional_object(payload: JsonObject, key: str) -> JsonObject | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(f"{key} must be an object or null")
    return dict(value)


def _require_enum(payload: JsonObject, key: str, allowed: set[str]) -> str:
    value = _require_string(payload, key)
    if value not in allowed:
        raise ValidationError(f"{key} has unsupported value: {value}")
    return value


def _require_schema_version(payload: JsonObject) -> int:
    version = _require_int(payload, "schema_version")
    if version != 1:
        raise ValidationError("schema_version must be 1")
    return version


def _validate_id(value: str, prefix: str, field_name: str) -> str:
    if not value.startswith(prefix):
        raise ValidationError(f"{field_name} must start with {prefix}")
    return value


def _validate_hash(value: str, field_name: str) -> str:
    if not value.startswith("sha256:"):
        raise ValidationError(f"{field_name} must start with sha256:")
    return value


def _validate_hub_path(value: str) -> str:
    if not value.startswith("/servers/") or not value.endswith("/mcp"):
        raise ValidationError("transport.hub_path must match /servers/<server>/mcp")
    return value


def _validate_safe_command(value: str | None, field_name: str) -> str | None:
    try:
        return validate_command_name(value, field_name=field_name)
    except SecurityError as error:
        raise ValidationError(str(error)) from error


def _validate_safe_http_url(value: str | None, field_name: str) -> str | None:
    try:
        return validate_http_url(value, field_name=field_name)
    except SecurityError as error:
        raise ValidationError(str(error)) from error


def _to_json_value(value: Any) -> Any:
    if isinstance(value, SchemaModel):
        return value.to_dict()
    if isinstance(value, list):
        return [_to_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class SchemaModel:
    """Base class for schema models."""

    schema_version: int = 1
    _optional_fields: ClassVar[set[str]] = set()
    _include_schema_version: ClassVar[bool] = True

    def to_dict(self) -> JsonObject:
        """Return a stable JSON-compatible dictionary."""
        result: JsonObject = {}
        for model_field in fields(self):
            if model_field.name == "schema_version" and not self._include_schema_version:
                continue
            result[model_field.name] = _to_json_value(getattr(self, model_field.name))
        return result

    @classmethod
    def _reject_unknown_keys(cls, payload: JsonObject) -> None:
        accepted = {field.name for field in fields(cls)}
        unknown = set(payload) - accepted
        if unknown:
            raise ValidationError(f"unknown fields for {cls.__name__}: {sorted(unknown)}")

    @classmethod
    def from_dict(cls: type[T], payload: JsonObject) -> T:
        raise NotImplementedError


@dataclass(frozen=True)
class ToolFilters(SchemaModel):
    _include_schema_version: ClassVar[bool] = False

    enabled_tools: list[str] | None = None
    disabled_tools: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: JsonObject) -> ToolFilters:
        cls._reject_unknown_keys(payload)
        _require_schema_version({"schema_version": payload.get("schema_version", 1)})
        enabled = payload.get("enabled_tools")
        if enabled is not None and (
            not isinstance(enabled, list) or not all(isinstance(item, str) for item in enabled)
        ):
            raise ValidationError("enabled_tools must be a list of strings or null")
        disabled = payload.get("disabled_tools", [])
        if not isinstance(disabled, list) or not all(isinstance(item, str) for item in disabled):
            raise ValidationError("disabled_tools must be a list of strings")
        return cls(enabled_tools=enabled, disabled_tools=disabled)


@dataclass(frozen=True)
class ObservedEntry(SchemaModel):
    observed_entry_id: str = ""
    agent_id: str = ""
    agent_kind: str = ""
    config_path: str = ""
    container_path: list[str] = field(default_factory=list)
    mount_name: str = ""
    enabled: bool = True
    transport: str = ""
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    headers_present: list[str] = field(default_factory=list)
    env_names: list[str] = field(default_factory=list)
    cwd: str | None = None
    tool_filters: JsonObject = field(default_factory=dict)
    approval_policy: str | None = None
    entry_hash: str = ""
    raw_shape: str = ""
    parser_confidence: str = ""

    @classmethod
    def from_dict(cls, payload: JsonObject) -> ObservedEntry:
        cls._reject_unknown_keys(payload)
        schema_version = _require_schema_version(payload)
        transport = _require_enum(payload, "transport", TRANSPORTS)
        command = _validate_safe_command(_optional_string(payload, "command"), "command")
        url = _validate_safe_http_url(_optional_string(payload, "url"), "url")
        if transport == "stdio" and not command:
            raise ValidationError("stdio observed entries require command")
        if transport != "stdio" and not url:
            raise ValidationError("non-stdio observed entries require url")
        return cls(
            schema_version=schema_version,
            observed_entry_id=_validate_id(
                _require_string(payload, "observed_entry_id"), "obs_", "observed_entry_id"
            ),
            agent_id=_require_string(payload, "agent_id"),
            agent_kind=_require_enum(payload, "agent_kind", AGENT_KINDS),
            config_path=_require_string(payload, "config_path"),
            container_path=_require_string_list(payload, "container_path"),
            mount_name=_require_string(payload, "mount_name"),
            enabled=_require_bool(payload, "enabled"),
            transport=transport,
            command=command,
            args=_require_string_list(payload, "args"),
            url=url,
            headers_present=_require_string_list(payload, "headers_present"),
            env_names=_require_string_list(payload, "env_names"),
            cwd=_optional_string(payload, "cwd"),
            tool_filters=ToolFilters.from_dict(_require_object(payload, "tool_filters")).to_dict(),
            approval_policy=_optional_string(payload, "approval_policy"),
            entry_hash=_validate_hash(_require_string(payload, "entry_hash"), "entry_hash"),
            raw_shape=_require_string(payload, "raw_shape"),
            parser_confidence=_require_enum(payload, "parser_confidence", PARSER_CONFIDENCE),
        )


@dataclass(frozen=True)
class BackendTransport(SchemaModel):
    _include_schema_version: ClassVar[bool] = False

    type: str = ""
    command: str | None = None
    args: list[str] = field(default_factory=list)
    cwd_policy: str = "none"
    env: list[str] = field(default_factory=list)
    url: str | None = None

    @classmethod
    def from_dict(cls, payload: JsonObject) -> BackendTransport:
        cls._reject_unknown_keys(payload)
        backend_type = _require_enum(payload, "type", {"stdio", "streamable_http", "http", "sse"})
        command = _validate_safe_command(
            _optional_string(payload, "command"), "transport.backend.command"
        )
        url = _validate_safe_http_url(_optional_string(payload, "url"), "transport.backend.url")
        if backend_type == "stdio" and not command:
            raise ValidationError("stdio backend transport requires command")
        if backend_type != "stdio" and not url:
            raise ValidationError("HTTP backend transport requires url")
        return cls(
            type=backend_type,
            command=command,
            args=_require_string_list({"args": payload.get("args", [])}, "args"),
            cwd_policy=str(payload.get("cwd_policy", "none")),
            env=_require_string_list({"env": payload.get("env", [])}, "env"),
            url=url,
        )


@dataclass(frozen=True)
class TransportConfig(SchemaModel):
    _include_schema_version: ClassVar[bool] = False

    frontend: str = ""
    hub_path: str = ""
    backend: BackendTransport = field(default_factory=BackendTransport)

    @classmethod
    def from_dict(cls, payload: JsonObject) -> TransportConfig:
        cls._reject_unknown_keys(payload)
        return cls(
            frontend=_require_enum(payload, "frontend", {"streamable_http"}),
            hub_path=_validate_hub_path(_require_string(payload, "hub_path")),
            backend=BackendTransport.from_dict(_require_object(payload, "backend")),
        )


@dataclass(frozen=True)
class RuntimeConfig(SchemaModel):
    _include_schema_version: ClassVar[bool] = False

    shareability: str = ""
    concurrency: str = ""
    idle_timeout_sec: int = 600
    health_check: str = ""

    @classmethod
    def from_dict(cls, payload: JsonObject) -> RuntimeConfig:
        cls._reject_unknown_keys(payload)
        return cls(
            shareability=_require_enum(payload, "shareability", SHAREABILITY),
            concurrency=_require_enum(payload, "concurrency", CONCURRENCY),
            idle_timeout_sec=_require_int(payload, "idle_timeout_sec", minimum=1),
            health_check=_require_string(payload, "health_check"),
        )


@dataclass(frozen=True)
class ActiveSetConfig(SchemaModel):
    _include_schema_version: ClassVar[bool] = False

    eligible_profiles: list[str] = field(default_factory=list)
    default_enabled: bool = False

    @classmethod
    def from_dict(cls, payload: JsonObject) -> ActiveSetConfig:
        cls._reject_unknown_keys(payload)
        return cls(
            eligible_profiles=_require_string_list(payload, "eligible_profiles"),
            default_enabled=_require_bool(payload, "default_enabled"),
        )


@dataclass(frozen=True)
class CatalogEntry(SchemaModel):
    catalog_id: str = ""
    name: str = ""
    canonical_name: str = ""
    family_id: str = ""
    variant_name: str | None = None
    display_label: str = ""
    aliases: list[str] = field(default_factory=list)
    review_state: str = ""
    lifecycle_state: str = ""
    risk_tier: str = ""
    provenance: list[JsonObject] = field(default_factory=list)
    transport: TransportConfig = field(default_factory=TransportConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    credentials: list[JsonObject] = field(default_factory=list)
    active_set: ActiveSetConfig = field(default_factory=ActiveSetConfig)

    @classmethod
    def from_dict(cls, payload: JsonObject) -> CatalogEntry:
        cls._reject_unknown_keys(payload)
        schema_version = _require_schema_version(payload)
        transport = TransportConfig.from_dict(_require_object(payload, "transport"))
        runtime = RuntimeConfig.from_dict(_require_object(payload, "runtime"))
        if runtime.shareability == "global" and transport.backend.type != "stdio":
            raise ValidationError("global shareability requires explicitly reviewed stdio backend")
        return cls(
            schema_version=schema_version,
            catalog_id=_validate_id(_require_string(payload, "catalog_id"), "srv_", "catalog_id"),
            name=_require_string(payload, "name"),
            canonical_name=_require_string(payload, "canonical_name"),
            family_id=_require_string(payload, "family_id"),
            variant_name=_optional_string(payload, "variant_name"),
            display_label=_require_string(payload, "display_label"),
            aliases=_require_string_list(payload, "aliases"),
            review_state=_require_enum(payload, "review_state", CATALOG_REVIEW_STATES),
            lifecycle_state=_require_enum(payload, "lifecycle_state", CATALOG_LIFECYCLE_STATES),
            risk_tier=_require_enum(payload, "risk_tier", RISK_TIERS - {"unknown"}),
            provenance=_require_list(payload, "provenance"),
            transport=transport,
            runtime=runtime,
            credentials=_require_list(payload, "credentials"),
            active_set=ActiveSetConfig.from_dict(_require_object(payload, "active_set")),
        )


@dataclass(frozen=True)
class CatalogCandidate(SchemaModel):
    candidate_id: str = ""
    source: str = ""
    observed_entry_id: str = ""
    proposed_name: str = ""
    classification: str = ""
    review_state: str = "pending"
    risk_tier: str = "unknown"
    confidence: str = "low"
    backend_shape: JsonObject = field(default_factory=dict)
    approval_required: bool = True
    reasons: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: JsonObject) -> CatalogCandidate:
        cls._reject_unknown_keys(payload)
        schema_version = _require_schema_version(payload)
        classification = _require_enum(payload, "classification", CANDIDATE_CLASSIFICATIONS)
        approval_required = _require_bool(payload, "approval_required")
        if classification.startswith("unknown_") and not approval_required:
            raise ValidationError("unknown candidates must require approval")
        return cls(
            schema_version=schema_version,
            candidate_id=_validate_id(
                _require_string(payload, "candidate_id"), "cand_", "candidate_id"
            ),
            source=_require_string(payload, "source"),
            observed_entry_id=_validate_id(
                _require_string(payload, "observed_entry_id"), "obs_", "observed_entry_id"
            ),
            proposed_name=_require_string(payload, "proposed_name"),
            classification=classification,
            review_state=_require_enum(payload, "review_state", CATALOG_REVIEW_STATES),
            risk_tier=_require_enum(payload, "risk_tier", RISK_TIERS),
            confidence=_require_enum(payload, "confidence", CANDIDATE_CONFIDENCE),
            backend_shape=_require_object(payload, "backend_shape"),
            approval_required=approval_required,
            reasons=_require_string_list(payload, "reasons"),
        )


@dataclass(frozen=True)
class DiffPayload(SchemaModel):
    _include_schema_version: ClassVar[bool] = False

    format: str = "unified"
    text: str = ""

    @classmethod
    def from_dict(cls, payload: JsonObject) -> DiffPayload:
        cls._reject_unknown_keys(payload)
        diff_format = _require_enum(payload, "format", {"unified"})
        return cls(format=diff_format, text=_require_string(payload, "text"))


@dataclass(frozen=True)
class RemediationPlan(SchemaModel):
    plan_id: str = ""
    plan_type: str = ""
    status: str = ""
    agent_id: str = ""
    target_path: str = ""
    observed_entry_id: str = ""
    catalog_id: str = ""
    policy: JsonObject = field(default_factory=dict)
    diff: DiffPayload = field(default_factory=DiffPayload)
    expected_preimage_hash: str = ""
    rollback_strategy: str = ""
    risk: JsonObject = field(default_factory=dict)
    created_at: str = ""

    @classmethod
    def from_dict(cls, payload: JsonObject) -> RemediationPlan:
        cls._reject_unknown_keys(payload)
        policy = _require_object(payload, "policy")
        if policy.get("approval_required") is True and not policy.get("approval_reason"):
            raise ValidationError("approval_required plans need approval_reason")
        return cls(
            schema_version=_require_schema_version(payload),
            plan_id=_validate_id(_require_string(payload, "plan_id"), "plan_", "plan_id"),
            plan_type=_require_enum(payload, "plan_type", PLAN_TYPES),
            status=_require_enum(payload, "status", PLAN_STATUSES),
            agent_id=_require_string(payload, "agent_id"),
            target_path=_require_string(payload, "target_path"),
            observed_entry_id=_validate_id(
                _require_string(payload, "observed_entry_id"), "obs_", "observed_entry_id"
            ),
            catalog_id=_validate_id(_require_string(payload, "catalog_id"), "srv_", "catalog_id"),
            policy=policy,
            diff=DiffPayload.from_dict(_require_object(payload, "diff")),
            expected_preimage_hash=_validate_hash(
                _require_string(payload, "expected_preimage_hash"), "expected_preimage_hash"
            ),
            rollback_strategy=_require_string(payload, "rollback_strategy"),
            risk=_require_object(payload, "risk"),
            created_at=_require_string(payload, "created_at"),
        )


@dataclass(frozen=True)
class Approval(SchemaModel):
    approval_id: str = ""
    plan_id: str = ""
    state: str = ""
    actor: str = ""
    channel: str = ""
    created_at: str = ""
    expires_at: str | None = None
    decision_at: str | None = None
    comment: str | None = None

    @classmethod
    def from_dict(cls, payload: JsonObject) -> Approval:
        cls._reject_unknown_keys(payload)
        state = _require_enum(payload, "state", APPROVAL_STATES)
        decision_at = _optional_string(payload, "decision_at")
        if state in {"approved", "rejected", "revoked"} and not decision_at:
            raise ValidationError("terminal approval states require decision_at")
        return cls(
            schema_version=_require_schema_version(payload),
            approval_id=_validate_id(
                _require_string(payload, "approval_id"), "appr_", "approval_id"
            ),
            plan_id=_validate_id(_require_string(payload, "plan_id"), "plan_", "plan_id"),
            state=state,
            actor=_require_string(payload, "actor"),
            channel=_require_string(payload, "channel"),
            created_at=_require_string(payload, "created_at"),
            expires_at=_optional_string(payload, "expires_at"),
            decision_at=decision_at,
            comment=_optional_string(payload, "comment"),
        )


@dataclass(frozen=True)
class AuditEvent(SchemaModel):
    event_id: str = ""
    event_type: str = ""
    actor: str = ""
    agent_id: str | None = None
    plan_id: str | None = None
    target_path: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    backup_id: str | None = None
    result: str = ""
    timestamp: str = ""
    redaction: str = "secret_values_removed"
    previous_event_hash: str | None = None
    event_hash: str = ""

    @classmethod
    def from_dict(cls, payload: JsonObject) -> AuditEvent:
        cls._reject_unknown_keys(payload)
        redaction = _require_string(payload, "redaction")
        if redaction != "secret_values_removed":
            raise ValidationError("audit events must declare secret value redaction")
        previous = _optional_string(payload, "previous_event_hash")
        before_hash = _optional_string(payload, "before_hash")
        after_hash = _optional_string(payload, "after_hash")
        if before_hash is not None:
            _validate_hash(before_hash, "before_hash")
        if after_hash is not None:
            _validate_hash(after_hash, "after_hash")
        if previous is not None:
            _validate_hash(previous, "previous_event_hash")
        return cls(
            schema_version=_require_schema_version(payload),
            event_id=_validate_id(_require_string(payload, "event_id"), "evt_", "event_id"),
            event_type=_require_string(payload, "event_type"),
            actor=_require_string(payload, "actor"),
            agent_id=_optional_string(payload, "agent_id"),
            plan_id=_optional_string(payload, "plan_id"),
            target_path=_optional_string(payload, "target_path"),
            before_hash=before_hash,
            after_hash=after_hash,
            backup_id=_optional_string(payload, "backup_id"),
            result=_require_string(payload, "result"),
            timestamp=_require_string(payload, "timestamp"),
            redaction=redaction,
            previous_event_hash=previous,
            event_hash=_validate_hash(_require_string(payload, "event_hash"), "event_hash"),
        )


@dataclass(frozen=True)
class RuntimeBackend(SchemaModel):
    backend_id: str = ""
    catalog_id: str = ""
    runtime_pool_key: str = ""
    state: str = ""
    pid: int | None = None
    account_scope: str | None = None
    workspace_root: str | None = None
    backend_initialize_count: int = 0
    frontend_session_count: int = 0
    created_at: str = ""
    last_used_at: str | None = None

    @classmethod
    def from_dict(cls, payload: JsonObject) -> RuntimeBackend:
        cls._reject_unknown_keys(payload)
        pid = payload.get("pid")
        if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
            raise ValidationError("pid must be a positive integer or null")
        return cls(
            schema_version=_require_schema_version(payload),
            backend_id=_validate_id(_require_string(payload, "backend_id"), "be_", "backend_id"),
            catalog_id=_validate_id(_require_string(payload, "catalog_id"), "srv_", "catalog_id"),
            runtime_pool_key=_require_string(payload, "runtime_pool_key"),
            state=_require_enum(payload, "state", RUNTIME_BACKEND_STATES),
            pid=pid,
            account_scope=_optional_string(payload, "account_scope"),
            workspace_root=_optional_string(payload, "workspace_root"),
            backend_initialize_count=_require_int(payload, "backend_initialize_count", minimum=0),
            frontend_session_count=_require_int(payload, "frontend_session_count", minimum=0),
            created_at=_require_string(payload, "created_at"),
            last_used_at=_optional_string(payload, "last_used_at"),
        )


@dataclass(frozen=True)
class HealthIssue(SchemaModel):
    _include_schema_version: ClassVar[bool] = False

    area: str = ""
    code: str = ""
    detail: str = ""
    agent_id: str | None = None
    server: str | None = None

    @classmethod
    def from_dict(cls, payload: JsonObject) -> HealthIssue:
        cls._reject_unknown_keys(payload)
        return cls(
            area=_require_enum(payload, "area", HEALTH_AREAS),
            code=_require_string(payload, "code"),
            detail=_require_string(payload, "detail"),
            agent_id=_optional_string(payload, "agent_id"),
            server=_optional_string(payload, "server"),
        )


@dataclass(frozen=True)
class HealthSummary(SchemaModel):
    _include_schema_version: ClassVar[bool] = False

    agents: int = 0
    blockers: int = 0
    warnings: int = 0
    notices: int = 0
    active_servers: int = 0
    hot_backends: int = 0
    pending_approvals: int = 0

    @classmethod
    def from_dict(cls, payload: JsonObject) -> HealthSummary:
        cls._reject_unknown_keys(payload)
        return cls(
            agents=_require_int(payload, "agents", minimum=0),
            blockers=_require_int(payload, "blockers", minimum=0),
            warnings=_require_int(payload, "warnings", minimum=0),
            notices=_require_int(payload, "notices", minimum=0),
            active_servers=_require_int(payload, "active_servers", minimum=0),
            hot_backends=_require_int(payload, "hot_backends", minimum=0),
            pending_approvals=_require_int(payload, "pending_approvals", minimum=0),
        )


@dataclass(frozen=True)
class HealthPayload(SchemaModel):
    kind: Literal["MCPMultiplexHealth"] = "MCPMultiplexHealth"
    ok: bool = True
    summary: HealthSummary = field(default_factory=HealthSummary)
    blockers: list[HealthIssue] = field(default_factory=list)
    warnings: list[HealthIssue] = field(default_factory=list)
    notices: list[HealthIssue] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: JsonObject) -> HealthPayload:
        cls._reject_unknown_keys(payload)
        schema_version = _require_schema_version(payload)
        kind = _require_string(payload, "kind")
        if kind not in get_args(Literal["MCPMultiplexHealth"]):
            raise ValidationError("kind must be MCPMultiplexHealth")
        ok = _require_bool(payload, "ok")
        summary = HealthSummary.from_dict(_require_object(payload, "summary"))
        blockers = [HealthIssue.from_dict(item) for item in _require_list(payload, "blockers")]
        warnings = [HealthIssue.from_dict(item) for item in _require_list(payload, "warnings")]
        notices = [HealthIssue.from_dict(item) for item in _require_list(payload, "notices")]
        if summary.blockers != len(blockers):
            raise ValidationError("summary.blockers must match blockers length")
        if summary.warnings != len(warnings):
            raise ValidationError("summary.warnings must match warnings length")
        if summary.notices != len(notices):
            raise ValidationError("summary.notices must match notices length")
        if ok and blockers:
            raise ValidationError("ok health payloads cannot contain blockers")
        return cls(
            schema_version=schema_version,
            kind="MCPMultiplexHealth",
            ok=ok,
            summary=summary,
            blockers=blockers,
            warnings=warnings,
            notices=notices,
        )
