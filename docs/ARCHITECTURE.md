# Target Architecture: MCP Multiplex

> **Implementation status:** The daemon, per-server runtime proxy, authenticated
> `mcp_hub` Streamable HTTP endpoint, five first-wave adapters, approval and
> rollback pipeline, CLI, and operator REPL are implemented. Real-client
> certification remains machine- and client-version-specific; use
> `mxp agents self-check` and the release gate before enabling automatic
> remediation. See [Control-Plane Protocol Status](CONTROL_PLANE_STATUS.md).

## 1. Architectural Decision

MCP Multiplex will be rebuilt as a **daemon-first local convergence system**.

The daemon is the source of truth for:

- catalog identity,
- desired active sets,
- observed agent config state,
- remediation planning,
- approval state,
- runtime mounts,
- backend process/session lifecycle,
- credential readiness,
- audit logs,
- health computation.

The CLI, TUI, and `mcp_hub` control-plane MCP are clients of the daemon.

## 2. Top-Level System

```text
Supported agents
  ├─ Codex
  ├─ Claude Code
  ├─ Gemini CLI
  ├─ Cline
  ├─ OpenCode
  └─ future clients
        |
        | named MCP entries
        v
http://127.0.0.1:30000/servers/<server>/mcp
        |
        v
MCP Multiplex Daemon
  ├─ agent registry
  ├─ config adapters
  ├─ file watchers
  ├─ catalog and identity engine
  ├─ active-set/profile engine
  ├─ compliance planner
  ├─ approval gate
  ├─ atomic config writer
  ├─ runtime proxy
  ├─ backend session manager
  ├─ credential resolver
  ├─ event/audit store
  └─ health engine
        |
        ├─ local stdio MCP backends
        ├─ remote HTTP MCP backends
        └─ legacy compatibility bridges
```

## 3. Network Contract

### 3.1 Data Plane

Every active data-plane MCP exposed to agents must use:

```text
http://127.0.0.1:30000/servers/<server>/mcp
```

The `<server>` segment is the agent-facing mount name. It must be stable within the active profile.

### 3.2 Control Plane

The daemon exposes a local authenticated API for:

- CLI,
- TUI,
- `mcp_hub` MCP server,
- internal diagnostics.

The control-plane MCP must not be the data-plane gateway for all downstream tools. It is for status, self-check, planning, and approved management.

## 4. Component Responsibilities

### 4.1 Daemon Supervisor

Responsibilities:

- start local service,
- load config,
- initialize database,
- start proxy listener,
- start file watchers,
- run periodic audits,
- coordinate shutdown,
- recover from crashes.

### 4.2 Agent Registry

Tracks supported agents.

Fields:

- `agent_id`,
- `agent_kind`,
- `display_name`,
- `workspace_root`,
- `config_paths`,
- `control_plane_mount`,
- `auth_token_ref`,
- `certification_level`,
- `created_at`,
- `last_seen_at`.

The registry is required because MCP itself does not standardize invoking-agent identity.

### 4.3 Config Adapters

One adapter per supported client.

Adapter interface:

```text
discover_paths(context) -> list[ConfigPath]
parse(path) -> ParsedAgentConfig
normalize(parsed) -> list[ObservedMcpEntry]
plan_rewrite(parsed, changes) -> ConfigEditPlan
serialize(parsed) -> bytes
validate(bytes) -> ValidationResult
supports_atomic_edit(path) -> bool
reload_hint() -> ReloadInstruction
```

Adapters must be fixture-tested with before/after configs.

### 4.4 Catalog Engine

Owns canonical identity and catalog state.

Responsibilities:

- match observed entries to catalog,
- create candidates,
- group aliases and variants,
- compute fingerprints,
- enforce required metadata before routing,
- track provenance,
- expose duplicate/variant review.

### 4.5 Active-Set Engine

Computes desired MCP entries for each agent/workspace.

Inputs:

- profiles,
- packs,
- catalog eligibility,
- approved extras,
- workspace policy,
- agent certification level.

Outputs:

- desired agent config projection,
- active server list,
- reasons each server is active,
- warnings/blockers for broad active sets.

### 4.6 Compliance Planner

Compares observed config against desired state.

Produces remediation plans:

- known direct rewrite,
- unknown import candidate,
- missing Hub control-plane entry,
- missing active server,
- accidental extra,
- duplicate alias,
- unsafe local HTTP endpoint,
- unsupported config.

### 4.7 Approval Gate

Owns approval lifecycle.

Approval states:

- `not_required`,
- `pending`,
- `approved`,
- `rejected`,
- `expired`,
- `applied`,
- `revoked`.

Destructive or ambiguous plans must wait for approval through CLI/TUI. Model-only approval through an agent is not sufficient.

### 4.8 Atomic Config Writer

Applies approved config mutations.

Algorithm:

1. Acquire path lock.
2. Read file.
3. Verify expected pre-image hash.
4. Parse.
5. Apply edit.
6. Serialize.
7. Validate syntax.
8. Write temp file in same directory.
9. Preserve mode/owner where possible.
10. `fsync`.
11. Atomic rename.
12. Re-read.
13. Verify post-image hash.
14. Emit audit event.
15. Roll back if validation fails.

### 4.9 Runtime Proxy

Handles agent MCP traffic for:

```text
/servers/<server>/mcp
```

Responsibilities:

- validate request origin/auth,
- map server name to catalog entry,
- create frontend session,
- choose backend runtime pool,
- start backend lazily,
- forward initialize/tools/resources/prompts,
- rewrite request IDs,
- scope notifications,
- enforce concurrency policy,
- handle cancellation,
- reap idle backends,
- emit runtime events.

### 4.10 Backend Session Manager

Chooses runtime isolation key.

Isolation dimensions:

- server canonical ID,
- shareability policy,
- workspace root,
- agent ID,
- account scope,
- remote URL,
- credentials,
- runtime variant.

No backend sharing occurs without explicit catalog shareability and acceptance evidence.

### 4.11 Credential Resolver

Responsibilities:

- track secret references,
- check readiness without values,
- resolve secrets only at backend startup or explicit auth flow,
- redact logs,
- handle locked/unavailable secret sources,
- maintain credential status.

### 4.12 Event and Audit Store

Stores:

- observed config events,
- drift events,
- remediation plans,
- approvals,
- config mutations,
- rollbacks,
- runtime starts/stops/reuse/crashes,
- credential readiness,
- security denials.

Audit logs must support "what changed my config?".

## 5. State Machine: Self-Healing Compliance

```text
OBSERVE
  -> PARSE
  -> CLASSIFY_ENTRY
  -> MATCH_CATALOG
  -> DECIDE_POLICY
  -> PLAN
  -> APPROVE_OR_AUTO
  -> BACKUP
  -> APPLY_ATOMIC
  -> VERIFY
  -> AUDIT
  -> ROLLBACK_ON_FAILURE
```

## 6. Runtime Session Model

### 6.1 Frontend Session

Represents one client-facing MCP session.

Fields:

- `frontend_session_id`,
- `agent_id`,
- `server_name`,
- `workspace_root`,
- `protocol_version`,
- `created_at`,
- `last_seen_at`.

### 6.2 Backend Session

Represents one managed backend MCP process or remote session.

Fields:

- `backend_session_id`,
- `canonical_server_id`,
- `runtime_pool_key`,
- `account_scope`,
- `workspace_root`,
- `pid` or remote session identifier,
- `initialize_result`,
- `state`,
- `created_at`,
- `last_used_at`.

### 6.3 Session Mapping

Mapping is allowed only when the catalog shareability policy permits it.

Examples:

- stateless docs server: many frontend sessions to one backend session,
- filesystem server: many frontends in same workspace to one backend session,
- browser server: per workspace or per agent,
- per-agent identity server: one backend per agent.

## 7. Health Computation

Health is computed from real machine state.

Blockers:

- active direct bypass,
- invalid active agent config,
- daemon/proxy unavailable,
- enabled active server cannot initialize,
- missing required credential for active server,
- unsafe local HTTP direct endpoint,
- failed config rewrite awaiting rollback.

Warnings:

- dormant credentials missing,
- unsupported observed client,
- pending candidate review,
- unmanaged MCP-like process,
- active-set count above warning threshold.

Notices:

- backend reaped,
- tool list changed,
- candidate discovered,
- profile extra observed.

## 8. Security Architecture

Security principles:

- local-only by default,
- explicit control-plane auth,
- origin validation,
- no raw secrets in primary config,
- no shell strings for imported commands by default,
- no destructive model-only approvals,
- isolated backend pools where needed,
- audit every mutation,
- preserve rollback paths.

## 9. Migration Architecture

The old `mcp-hub` state must be imported as untrusted legacy provenance.

Migration phases:

1. Observe current configs.
2. Import current Hub catalog as candidates/provenance.
3. Build normalized inventory.
4. Generate dry-run remediation plans.
5. Certify adapters.
6. Apply known safe rewrites.
7. Enable automatic remediation for certified safe cases.
