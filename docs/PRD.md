# Product Requirements Document: MCP Multiplex

## 1. Document Purpose

This PRD defines the complete product contract for rebuilding MCP Hub as MCP Multiplex.

The goal is to create an unambiguous execution target for engineering, testing, and operations. This document should be treated as the source of truth for what the rebuilt application must do, what it must not do, how success is measured, and what workflows must work end to end.

## 2. Product Name

**MCP Multiplex**

Rationale: the application governs the local MCP ecosystem. It is not merely a hub, proxy, launcher, or catalog. Its primary job is policy-driven convergence across agents, configs, runtimes, credentials, and operator workflows.

## 3. Problem Statement

Modern coding agents and desktop AI clients support MCP servers, but each client manages MCP configuration independently. On a single developer machine, this creates a recurring operational failure mode:

- agents install MCP servers directly,
- multiple agents configure the same MCP under different names,
- configs drift across Codex, Claude, Gemini, Cline, OpenCode, Cursor, and other clients,
- each client may spawn its own backend process,
- eager clients initialize too many MCPs at startup,
- credentials are duplicated or scattered,
- local HTTP MCP endpoints bypass governance,
- stale MCP-like processes remain running,
- operators cannot quickly answer why an agent is slow, broken, or bypassing policy,
- tests can pass while the actual machine remains unhealthy.

The existing `mcp-hub` implementation proved the value of a local control plane but accumulated too much drift and ambiguity. MCP Multiplex rebuilds the system around a daemon-first convergence model with explicit schemas, policies, approvals, runtime isolation, and acceptance tests.

## 4. Product Thesis

MCP Multiplex is a machine-wide local control plane, catalog, policy engine, and runtime proxy for MCP servers used by coding agents.

The machine should continuously converge to this state:

```text
Every supported agent has the mcp_hub control-plane MCP installed.
Every active data-plane MCP entry routes through a Hub-owned per-server URL.
Known direct MCP entries are rewritten through the Hub when safe.
Unknown direct MCP entries are classified and staged before trust.
Backend MCP servers are started lazily and shared only when safe.
Credentials are referenced, checked, and resolved without leaking values.
Operator-visible health reflects the real machine state.
Every mutation is backed up, auditable, and reversible.
```

The system must preserve named downstream MCP boundaries. Agents should still see normal server names such as `context7`, `playwright`, `github`, `filesystem`, and `chrome-devtools`. MCP Multiplex must not hide all downstream tools behind one omnibus MCP.

## 5. Primary Users

### 5.1 Local Operator

The local operator is the developer or power user who owns the machine. They need to inspect, approve, repair, and understand MCP state.

Primary needs:

- see if the machine is healthy,
- know which agents are compliant,
- approve risky imports or rewrites,
- roll back config mutations,
- understand startup slowness,
- inspect active backend processes,
- resolve credential readiness,
- manage catalog candidates and duplicates.

### 5.2 Coding Agent

A supported coding agent such as Codex, Claude Code, Gemini, Cline, or OpenCode uses MCP entries during work.

Primary needs:

- discover normal named MCP tools,
- access MCPs through stable Hub URLs,
- invoke `mcp_hub` for self-check/status/plans,
- avoid broken or excessive MCP startup,
- avoid config drift that silently bypasses the Hub.

### 5.3 Maintainer / Automation

The maintainer or automation pipeline validates the application.

Primary needs:

- run deterministic tests,
- validate schemas,
- run real-client acceptance fixtures,
- inspect logs and audit events,
- safely migrate existing configs.

## 6. Non-Goals

MCP Multiplex must not:

- become an omnibus MCP-only data plane,
- assume all MCP backends are safely shareable,
- store raw secrets in primary config,
- silently kill unmanaged processes,
- silently prune user-active tools,
- rewrite unsupported or opaque config formats,
- route unknown local HTTP endpoints without classification and approval,
- rely only on fake backend tests for release confidence,
- require every catalog entry to be active in every agent,
- treat agent config as source of truth,
- skip backups for config mutations,
- hide destructive actions behind model-only approval.

## 7. Core Product Concepts

### 7.1 Catalog

The catalog is the broad inventory of all known MCP servers. It includes approved servers, disabled servers, staged candidates, quarantined observations, duplicate variants, remote servers, local stdio servers, and legacy imports.

Catalog does not mean active.

### 7.2 Active Set

The active set is the small role-specific subset of catalog entries that should appear in an agent’s config.

Active sets are produced by profiles, packs, workspace policy, and approved extras.

### 7.3 Agent Config Projection

Agent config is an output projection of desired Hub state plus controlled user overrides. It is not the authority.

The daemon continuously compares observed config against desired state.

### 7.4 Direct Bypass

A direct bypass is any active MCP entry in a supported agent config that does not route through the Hub per-server URL contract.

Examples:

```text
command = "npx"
args = ["-y", "@upstash/context7-mcp"]
```

```text
url = "http://127.0.0.1:3845/mcp"
```

### 7.5 Hub-Routed Entry

A compliant data-plane MCP entry points to:

```text
http://127.0.0.1:30000/servers/<server>/mcp
```

The agent-facing server name remains the normal MCP name.

### 7.6 Control-Plane MCP

`mcp_hub` is the control-plane MCP installed in supported agents. It does not re-export all downstream tools. It allows agents to request self-checks, inspect plans, ask why a server is unavailable, and surface remediation to the operator.

### 7.7 Runtime Pool

A runtime pool is the set of backend process/session instances managed by the daemon for a given server under an isolation key.

Isolation key examples:

- global server,
- workspace root,
- agent ID,
- account scope,
- remote provider URL.

## 8. Required Product Behavior

### 8.1 Continuous Convergence

MCP Multiplex must continuously converge supported agent configs toward desired state.

Triggers:

- daemon file watcher on known config paths,
- periodic audit,
- daemon startup audit,
- `mcp_hub.self_check` invoked by an agent,
- operator-triggered audit,
- profile/catalog/credential state change.

Required behavior:

1. Observe config or runtime state.
2. Parse with the relevant adapter.
3. Normalize to observed MCP entries.
4. Classify each entry.
5. Match entries to catalog.
6. Decide policy.
7. Create a remediation plan.
8. Auto-apply only if safe and allowed.
9. Otherwise create an approval task.
10. Backup before mutation.
11. Apply atomically.
12. Verify.
13. Audit.
14. Roll back on validation failure.

### 8.2 Known Direct MCP Rewrite

When a supported agent config contains a direct MCP entry that confidently matches an approved catalog entry, MCP Multiplex must rewrite it through the Hub if policy allows.

Example before:

```toml
[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]
```

Example after:

```toml
[mcp_servers.context7]
url = "http://127.0.0.1:30000/servers/context7/mcp"
```

Requirements:

- preserve server name when possible,
- preserve tool allow/deny fields,
- preserve approval fields,
- preserve disabled state,
- preserve user intent,
- move backend command/env/cwd into Hub catalog/provenance where appropriate,
- backup original config,
- verify no direct active entry remains.

### 8.3 Unknown MCP Import and Staging

When an agent installs a direct MCP unknown to the catalog, MCP Multiplex must not blindly trust it.

Required behavior:

- create a catalog candidate,
- classify transport shape,
- capture provenance,
- infer risk,
- infer required credentials if possible,
- determine whether probing is safe,
- stage as disabled/pending by default,
- surface approval task,
- do not rewrite active config unless policy classifies the candidate as safe.

Unknown local HTTP endpoints require special caution. They must not be routed through the Hub until process ownership, bind address, transport shape, and risk are understood or explicitly approved.

### 8.4 Active-Set Governance

MCP Multiplex must prevent active agent configs from growing without bound.

Required behavior:

- define profiles and packs,
- support approved profile extras,
- detect accidental extras,
- warn when active count exceeds policy threshold,
- block or require approval above hard threshold,
- keep catalog inventory separate from active config,
- explain why each active server is present.

Initial default thresholds:

- target active set: 3-7 servers,
- warning above 10 active servers,
- blocker above 15 active servers unless explicit override exists.

These thresholds must be empirically validated.

### 8.5 Runtime Proxy

MCP Multiplex must expose each data-plane server at:

```text
http://127.0.0.1:30000/servers/<server>/mcp
```

Required behavior:

- validate local auth and origin where applicable,
- map frontend sessions to backend sessions according to policy,
- start backends lazily,
- cache initialize results only where safe,
- rewrite JSON-RPC request IDs,
- forward or scope notifications,
- support session deletion,
- support idle reaping,
- detect backend crashes,
- restart lazily if policy allows,
- emit runtime events.

### 8.6 Backend Sharing and Isolation

MCP Multiplex must never infer backend shareability from transport alone.

Each catalog entry must declare a shareability policy:

- global share,
- per workspace,
- per agent,
- per account,
- isolated per frontend session,
- no proxy/share allowed.

Default stance for unknown servers: not shareable.

### 8.7 Credential Readiness

MCP Multiplex must distinguish credential metadata from secret values.

Required behavior:

- store secret references only,
- support readiness checks that do not reveal values,
- avoid triggering passphrase prompts during broad inventory,
- resolve secrets only when starting a backend or performing explicitly authorized auth flow,
- show credential state as `present`, `missing`, `locked`, `expired`, `source_unavailable`, or `permission_denied`,
- treat missing active credentials as blockers,
- treat missing dormant credentials as warnings/notices.

### 8.8 Operator Health

Health must reflect actual local machine readiness, not just test status.

Health levels:

- `healthy`: active configs route through Hub and required active backends are ready,
- `warning`: non-blocking drift, dormant credential gaps, disabled candidates, unsupported observed clients,
- `blocker`: active direct bypass, invalid config, missing active credentials, unsafe active local HTTP endpoint, daemon/proxy unavailable,
- `notice`: new candidate, backend reaped, tool-list changed, profile extra observed.

### 8.9 Approvals

MCP Multiplex must separate plan generation from mutation.

Automatic actions are allowed only for certified adapters and safe policy cases.

Approval-gated actions include:

- enabling unknown MCPs,
- routing unknown local HTTP MCPs,
- modifying project-shared config,
- pruning active extras,
- killing unmanaged MCP-like processes,
- changing credential sources,
- rewriting ambiguous env/cwd/account variants.

Forbidden actions include:

- modifying enterprise-managed config,
- rewriting unsupported opaque config,
- executing shell-string commands as imported backends without review,
- storing raw secrets in primary config,
- killing unknown owner/system processes.

## 9. Supported Client Requirements

### 9.1 Client Certification Levels

Each client adapter must be classified:

- `certified`: automatic remediation allowed for safe cases,
- `best_effort`: detection and planning allowed; apply requires approval,
- `unverified`: read-only discovery only,
- `unsupported`: ignored except for operator notice.

### 9.2 First-Wave Certified Targets

First-wave targets after verification:

- Codex CLI,
- Claude Code,
- Gemini CLI,
- Cline,
- OpenCode.

### 9.3 Verification-Required Targets

Require certification before auto-rewrite:

- Codex Desktop if config differs from Codex CLI,
- Claude Desktop,
- Cursor,
- Antigravity/Gemini variants,
- DeepSeek-like clients.

### 9.4 Adapter Requirements

Each adapter must define:

- config paths,
- path precedence,
- parser,
- serializer,
- normalized observed entry schema,
- rewrite capability,
- syntax validation,
- live reload behavior,
- restart requirement,
- unsupported field behavior,
- backup/rollback semantics,
- fixture tests.

## 10. User Stories

### 10.1 Known Direct Entry Self-Heals

As a local operator, when Codex directly installs `context7`, I want MCP Multiplex to detect that `context7` already exists in the catalog and rewrite Codex to use the Hub URL, so Codex stops spawning its own backend.

Acceptance:

- direct entry is detected,
- catalog match is high-confidence,
- config is backed up,
- Codex config is rewritten,
- operator audit shows before/after,
- `context7` routes through the Hub,
- no secret values are logged.

### 10.2 Unknown Direct Entry Is Staged

As a local operator, when an agent installs a new MCP that is not in the catalog, I want it staged as a candidate rather than trusted automatically, so I can decide whether to approve it.

Acceptance:

- unknown entry remains visible,
- candidate is created,
- risk and provenance are shown,
- config is not silently rewritten if unsafe,
- TUI shows approval task.

### 10.3 Agent Calls Self-Check

As a coding agent, I want to call `mcp_hub.self_check` at session start, so I can know whether my MCP config is compliant and what remediation is available.

Acceptance:

- response is scoped to the invoking agent,
- no cross-agent mutation occurs,
- safe read-only status is returned,
- remediation plan IDs are returned,
- destructive actions require operator approval.

### 10.4 Operator Approves Rewrite

As an operator, I want to approve an ambiguous rewrite from the TUI, so MCP Multiplex can update the config safely.

Acceptance:

- TUI shows diff,
- TUI shows risk and reason,
- approval records actor and timestamp,
- apply is atomic,
- verification passes,
- rollback is available.

### 10.5 Hot Reuse Works Only When Safe

As an operator, I want stateless MCP backends to be reused across agents, but browser or workspace-sensitive tools isolated, so I get performance without state bleed.

Acceptance:

- stateless server shows backend reuse event,
- browser server uses workspace or agent isolation key,
- runtime status explains why each backend was or was not shared.

### 10.6 Why Is My Agent Slow

As an operator, I want one command or TUI view that explains why an agent is slow.

Acceptance:

- shows active MCP count,
- shows eager-init likely servers,
- shows backend cold-start timings,
- shows tool counts,
- shows hot reuse misses,
- recommends concrete actions.

### 10.7 What Changed My Config

As an operator, I want to see exactly what changed an agent config.

Acceptance:

- audit event shows actor, trigger, plan ID, diff, backup path, verification result,
- rollback command is provided,
- no raw secrets are shown.

## 11. Functional Requirements

### 11.1 Daemon

The daemon must:

- run as a local user service,
- bind proxy to `127.0.0.1` by default,
- watch known config paths,
- run periodic audits,
- expose local API for CLI/TUI/control-plane MCP,
- own catalog state,
- own runtime state,
- own approval state,
- own audit state,
- compute health,
- coordinate config mutations,
- coordinate backend runtime lifecycle,
- survive restart without losing durable plans or audit data.

### 11.2 Catalog

The catalog system must:

- store approved servers,
- store disabled servers,
- store candidates,
- store rejected candidates,
- store aliases,
- store variants,
- store family relationships,
- store provenance,
- store risk tier,
- store credential requirements,
- store runtime shareability,
- store active-set eligibility,
- reject routing for entries missing required metadata.

### 11.3 Agent Adapters

Each adapter must:

- discover config paths,
- parse config,
- normalize MCP entries,
- detect Hub-routed entries,
- detect direct entries,
- preserve unsupported fields,
- generate exact rewrite plans,
- validate serialized output,
- report whether live rewrite is safe,
- specify restart/reload instructions.

### 11.4 Compliance Planner

The planner must:

- compare observed entries to desired state,
- generate remediation plans,
- classify confidence,
- classify risk,
- determine approval requirements,
- generate diffs,
- avoid mutation when parser confidence is incomplete,
- avoid repeated rewrite loops,
- expose plans to CLI/TUI/control-plane MCP.

### 11.5 Atomic Apply

The apply system must:

- require a plan,
- require approval where policy demands it,
- acquire file lock,
- validate pre-image hash,
- create backup,
- write atomically,
- validate syntax,
- verify post-state,
- emit audit event,
- roll back automatically on validation failure.

### 11.6 Runtime Proxy

The runtime proxy must:

- implement MCP Streamable HTTP data-plane routing,
- support local stdio backend transport,
- support remote HTTP backend transport,
- support legacy compatibility only where explicitly configured,
- manage frontend sessions,
- manage backend sessions,
- select backend pool by shareability policy,
- rewrite JSON-RPC IDs,
- enforce concurrency policy,
- support cancellation where possible,
- emit runtime events,
- expose runtime status.

### 11.7 Credentials

The credential system must:

- store references only,
- check readiness without values,
- avoid passphrase prompts during inventory,
- resolve secrets only at backend startup or explicit auth flow,
- redact values from logs,
- classify missing active credentials as blockers,
- classify missing dormant credentials as non-blocking,
- support guided setup without writing raw secrets to primary config.

### 11.8 CLI

The CLI must support:

- compact status,
- JSON status,
- audit run,
- plan list/show,
- approval list/approve/reject,
- apply,
- rollback,
- catalog list/candidates/duplicates,
- runtime status,
- why-slow diagnostics,
- release gate.

### 11.9 TUI

The TUI must support:

- dashboard,
- blockers/warnings/notices,
- approvals,
- plan diff review,
- candidate review,
- credential readiness,
- runtime view,
- rollback view,
- why-slow view,
- what-changed view.

The TUI must not require reading raw JSON for normal triage.

### 11.10 Control-Plane MCP

The `mcp_hub` MCP must expose:

- `self_check`,
- `status`,
- `plan_list`,
- `plan_show`,
- `proxy_url`,
- `runtime_status`,
- `credential_status`,
- `catalog_search`.

It must not expose:

- raw secrets,
- unilateral destructive apply,
- process killing,
- cross-agent mutation unless policy explicitly allows it.

## 12. Nonfunctional Requirements

### 12.1 Reliability

- Daemon restart must not corrupt config or state.
- Config writes must be atomic.
- Backend crashes must not crash the daemon.
- Failed rewrites must roll back.
- File watchers must have periodic audit fallback.
- Unsupported configs must degrade to plan-only/read-only behavior.

### 12.2 Safety

- Unknown entries default to disabled or pending.
- Direct active bypasses are blockers.
- Process killing is never automatic.
- Project-shared config mutation requires approval.
- Enterprise-managed config is plan-only or forbidden.
- Model-only approval is insufficient for destructive operations.

### 12.3 Security

- Bind local service to `127.0.0.1` by default.
- Validate `Origin` for HTTP requests.
- Require local auth for control-plane mutation APIs.
- Separate data-plane and control-plane permissions.
- Store agent tokens securely.
- Validate remote URLs.
- Reject dangerous command imports.
- Redact secrets.
- Maintain tamper-evident audit logs.

### 12.4 Performance

- Compact status should return in under 500 ms on normal machines after warm state.
- Full audit should be incremental where possible.
- Runtime proxy overhead should be small relative to backend tool execution.
- Large catalogs should remain queryable without dumping full JSON to users.

### 12.5 Maintainability

- Each client adapter must have fixtures.
- Each schema must be versioned.
- Every mutation path must have tests.
- Every supported client must have an acceptance fixture.
- The daemon API must be documented.

## 13. Success Metrics

### 13.1 Product Metrics

- Direct active bypass count reaches zero for certified clients.
- Time to convergence after direct MCP drift is under 30 seconds for known safe entries.
- Unknown direct MCPs are staged within 30 seconds of observation.
- Backend reuse ratio is measurable and positive for shareable servers.
- Non-shareable servers are never shared across isolation boundaries.
- Operator can answer "what changed my config?" from audit logs.
- Operator can roll back config mutation with one command.

### 13.2 Release Metrics

- All schema tests pass.
- All adapter fixture tests pass.
- All atomic write and rollback tests pass.
- All runtime proxy tests pass.
- First-wave real-client acceptance tests pass.
- No secret values appear in logs.
- No destructive control-plane MCP action can execute without approval.

## 14. Initial Supported Clients

### 14.1 Certified First Wave After Verification

- Codex CLI,
- Claude Code,
- Gemini CLI,
- Cline,
- OpenCode.

### 14.2 Best-Effort / Verification Required

- Claude Desktop,
- Codex Desktop if separate from Codex CLI/IDE,
- Cursor,
- Gemini/Antigravity variants,
- DeepSeek-like clients.

## 15. Open Questions

- What is the exact Codex Desktop config contract, if distinct from Codex CLI?
- What is the current Cursor MCP config contract?
- What is the current Antigravity MCP config contract?
- Which remote MCP providers deviate from standard Streamable HTTP session behavior?
- What active-set threshold is empirically safe on target machines?
- Which MCP servers are truly global-shareable after real tests?
- Which OS keychain integrations should be first-class?

## 16. Risks

- Agents regenerate configs and fight Hub rewrites.
- Client config formats change.
- Backend sharing leaks state across agents.
- Unknown MCP imports introduce supply-chain risk.
- Local HTTP exposure expands attack surface.
- Secret readiness accidentally prompts or leaks.
- Operator distrust grows if rewrites are surprising.
- Big-bang migration breaks active workflows.

## 17. Release Criteria

MCP Multiplex is not release-ready until:

- the daemon can run in observe-only mode,
- first-wave adapters parse fixtures,
- known direct rewrites work with backup and rollback,
- unknown direct entries stage safely,
- per-server runtime proxy passes fake backend tests,
- hot reuse and non-shareable isolation both pass,
- credential readiness avoids secret prompts,
- TUI can show and approve plans,
- `mcp_hub.self_check` is scoped to invoking agent,
- real Codex, Claude Code, Gemini, Cline, and OpenCode acceptance tests pass,
- release gate fails on any active direct bypass.

## 18. Final Product Success Definition

MCP Multiplex succeeds when every supported coding agent can install or discover MCPs freely, but the machine continuously converges back to a governed state where known MCPs are cataloged once, exposed through Hub-managed per-server URLs, started only when needed, shared only when safe, credentialed without leaks, and explained clearly to the operator.
