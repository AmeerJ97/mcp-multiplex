# PRD Saga Checklist Roadmap: MCP Multiplex

## 1. Purpose

This document converts the MCP Multiplex PRD into an executable saga roadmap using the `PRD -> EPIC -> FEAT -> TASK -> CHECK` model.

It is designed for fresh-session development. A new agent should be able to open this file, follow dependencies, inspect referenced source docs, implement one task packet at a time, verify it, checkpoint it, and continue without rediscovering the product intent.

## 2. Source Of Truth

Primary docs:

- Product contract: [`docs/PRD.md`](PRD.md)
- Target architecture: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)
- Implementation phases: [`docs/IMPLEMENTATION_BLUEPRINT.md`](IMPLEMENTATION_BLUEPRINT.md)
- Schemas and payload contracts: [`docs/SCHEMAS_AND_CONTRACTS.md`](SCHEMAS_AND_CONTRACTS.md)
- Acceptance tests: [`docs/ACCEPTANCE_TEST_PLAN.md`](ACCEPTANCE_TEST_PLAN.md)

Decision boundaries:

- Preserve per-server MCP entries and per-server Hub URLs.
- Do not build an omnibus-only data plane.
- Do not silently mutate unsupported agent configs.
- Do not store raw secrets in primary config.
- Do not enable automatic remediation until the adapter and acceptance tests are certified.
- Do not treat fake backend tests as sufficient for release.

## 3. Three-Level Product Roadmap

### EPIC-01: Project Foundation And Durable State

Outcome: create a clean, testable project foundation with daemon, storage, schemas, and health baseline.

#### EPIC-01/FEAT-01: Repository And Runtime Skeleton

- TASK-001: initialize project structure, package manager, dev commands, lint/type/test harness.
- TASK-002: implement daemon health service and local CLI health client.
- TASK-003: implement configuration loading and environment layout.

#### EPIC-01/FEAT-02: Durable Schema And Audit Store

- TASK-004: implement database migrations and core tables.
- TASK-005: implement schema models and validation.
- TASK-006: implement append-only audit/event writer with redaction.

### EPIC-02: Agent Observation And Config Normalization

Outcome: observe supported agent configs without mutation and normalize every MCP entry into one internal shape.

#### EPIC-02/FEAT-01: Agent Registry

- TASK-007: implement agent registration model and identity hints.
- TASK-008: implement discovery of known config paths.

#### EPIC-02/FEAT-02: First-Wave Config Adapters

- TASK-009: implement Codex adapter fixtures and parser.
- TASK-010: implement Claude Code adapter fixtures and parser.
- TASK-011: implement Gemini CLI adapter fixtures and parser.
- TASK-012: implement Cline adapter fixtures and parser.
- TASK-013: implement OpenCode adapter fixtures and parser.

### EPIC-03: Read-Only Audit, Catalog Matching, And Candidate Staging

Outcome: detect drift and unknown MCPs safely without writing any config.

#### EPIC-03/FEAT-01: Read-Only Audit

- TASK-014: implement observed-entry ingestion and health classification.
- TASK-015: implement daemon file watcher with periodic audit fallback.

#### EPIC-03/FEAT-02: Catalog Identity And Matching

- TASK-016: implement catalog entry storage and required metadata validation.
- TASK-017: implement fingerprinting, alias matching, URL normalization, and confidence scoring.
- TASK-018: implement candidate staging for unknown stdio and local HTTP MCPs.

### EPIC-04: Remediation Planning, Approval, And Atomic Apply

Outcome: create exact plans and safely apply approved/certified config rewrites with backups and rollback.

#### EPIC-04/FEAT-01: Remediation Planner

- TASK-019: implement plan model, policy engine, and before/after diffs.
- TASK-020: implement self-healing state machine through dry-run planning.

#### EPIC-04/FEAT-02: Approval And Apply

- TASK-021: implement approval lifecycle and CLI commands.
- TASK-022: implement atomic config writer, backup metadata, verification, and rollback.
- TASK-023: enable auto-apply only for certified safe known-direct rewrites.

### EPIC-05: Runtime Proxy And Session Isolation

Outcome: route MCP traffic through per-server Hub URLs and share backends only where policy allows.

#### EPIC-05/FEAT-01: Data-Plane Proxy

- TASK-024: implement `/servers/<server>/mcp` Streamable HTTP frontend.
- TASK-025: implement local stdio backend transport.
- TASK-026: implement remote HTTP backend transport.

#### EPIC-05/FEAT-02: Runtime Pooling

- TASK-027: implement frontend/backend session mapping and request ID rewriting.
- TASK-028: implement shareability policy, isolation keys, and hot reuse metrics.
- TASK-029: implement crash recovery, idle reaping, cancellation handling, and runtime events.

### EPIC-06: Credentials, Security, And Policy Enforcement

Outcome: make the control plane safe enough to own local config and backend startup.

#### EPIC-06/FEAT-01: Credential Readiness

- TASK-030: implement secret reference schema and readiness states.
- TASK-031: implement env, `.env`, keychain placeholder, and `pass` no-prompt readiness checks.
- TASK-032: implement backend-startup secret resolution with redaction.

#### EPIC-06/FEAT-02: Security Controls

- TASK-033: implement local auth tokens, agent registration tokens, and permission scopes.
- TASK-034: implement Origin validation, URL validation, command import safety, and security denials.

### EPIC-07: Operator Surfaces

Outcome: provide CLI, TUI, and `mcp_hub` control-plane MCP workflows that are clear, safe, and non-destructive by default.

#### EPIC-07/FEAT-01: CLI

- TASK-035: implement compact status, JSON status, audit, plan, apply, rollback, and release-gate commands.

#### EPIC-07/FEAT-02: Control-Plane MCP

- TASK-036: implement `mcp_hub.self_check`, status, plan, proxy URL, runtime, credential, and catalog tools.

#### EPIC-07/FEAT-03: TUI

- TASK-037: implement dashboard, problems, approvals, candidate review, runtime, credentials, rollback, and why-slow views.

### EPIC-08: Real-Client Certification And Release Gate

Outcome: prove MCP Multiplex works with real supported clients and fails release on operational blockers.

#### EPIC-08/FEAT-01: Real-Client Acceptance

- TASK-038: certify Codex CLI.
- TASK-039: certify Claude Code.
- TASK-040: certify Gemini CLI.
- TASK-041: certify Cline.
- TASK-042: certify OpenCode.

#### EPIC-08/FEAT-02: Release Readiness

- TASK-043: implement release gate and migration dry-run.
- TASK-044: run end-to-end adversarial review and rollback drill.

## 4. Executable Task Packets

Grouped packet convention:

- Some implementation lanes are listed as `TASK-009 To TASK-013` or similar because the tasks share the same source docs, acceptance criteria, verification method, checkpoint, and git packet.
- Each task ID inside a grouped packet remains independently reviewable.
- When executing a grouped packet, implement one task ID at a time unless the user explicitly asks for the whole packet.
- Use the task split list inside the packet as the individual task descriptions.
- Use one semantic commit per individual task ID when the git metadata says "one commit per adapter", "one commit per surface", "one commit per client", or equivalent.

### TASK-001: Initialize Project Structure

Description: create the implementation repository skeleton for daemon, CLI, control MCP, adapters, catalog, runtime, credentials, storage, approvals, observability, TUI, and tests.

Priority: 100

Depends on: []

Workflow lane: foundation

Source docs:

- [`IMPLEMENTATION_BLUEPRINT.md`](IMPLEMENTATION_BLUEPRINT.md), Phase 1
- [`PRD.md`](PRD.md), sections 11 and 17

Implementation checks:

- [ ] Create package/runtime scaffold.
- [ ] Add source directories from blueprint.
- [ ] Add test fixture directories.
- [ ] Add lint, type, and test commands.
- [ ] Add developer README section for local commands.

Acceptance criteria:

- Project has executable dev commands.
- Empty test suite runs successfully.
- Directory structure matches blueprint.

Verification steps:

- Run project test command.
- Run lint command.
- Inspect tree for required directories.

Checkpoint:

- required: true
- artifact: scaffold diff, dev command output, notes on runtime choice
- review_gate: human review before schema implementation

Git:

- packet: P0 foundation
- branch_hint: task/TASK-001-project-skeleton
- commit_boundary: one commit for scaffold only
- source_hashes: []

### TASK-002: Daemon Health And CLI Health Client

Description: implement a minimal local daemon health endpoint and CLI client that can query it.

Priority: 99

Depends on: [TASK-001]

Workflow lane: foundation

Source docs:

- [`ARCHITECTURE.md`](ARCHITECTURE.md), sections 4.1 and 7
- [`SCHEMAS_AND_CONTRACTS.md`](SCHEMAS_AND_CONTRACTS.md), Health Payload

Implementation checks:

- [ ] Implement daemon process entrypoint.
- [ ] Bind to `127.0.0.1` by default.
- [ ] Implement `/healthz`.
- [ ] Implement CLI command for health.
- [ ] Add unit tests for healthy and daemon-unavailable states.

Acceptance criteria:

- CLI can report daemon health.
- Health response has stable schema.
- Daemon unavailable is handled as a blocker, not a crash.

Verification steps:

- Start daemon locally.
- Run CLI health command.
- Stop daemon and verify CLI reports unavailable.

Checkpoint:

- required: true
- artifact: daemon/CLI diff, health payload sample, test output
- review_gate: review before adding storage

Git:

- packet: P0 foundation
- branch_hint: task/TASK-002-daemon-health
- commit_boundary: one commit for daemon health and CLI health
- source_hashes: []

### TASK-003: Config Loading And Environment Layout

Description: implement initial config/state/cache directory resolution and declarative policy file loading.

Priority: 98

Depends on: [TASK-001, TASK-002]

Workflow lane: foundation

Source docs:

- [`PRD.md`](PRD.md), sections 7 and 11
- [`ARCHITECTURE.md`](ARCHITECTURE.md), section 4.1

Implementation checks:

- [ ] Resolve config dir.
- [ ] Resolve state dir.
- [ ] Resolve cache dir.
- [ ] Load empty/default policy config.
- [ ] Validate malformed config errors.

Acceptance criteria:

- Environment paths are deterministic.
- Config loading does not mutate files.
- Invalid config produces actionable error.

Verification steps:

- Run unit tests with temp home.
- Run CLI config inspect command.

Checkpoint:

- required: false
- artifact: test output
- review_gate: none

Git:

- packet: P0 foundation
- branch_hint: task/TASK-003-config-layout
- commit_boundary: one commit for config loading
- source_hashes: []

### TASK-004: Database Migrations And Core Tables

Description: implement SQLite migration harness and core tables for agents, catalog, observed entries, plans, approvals, runtime, credentials, events, and backups.

Priority: 97

Depends on: [TASK-003]

Workflow lane: storage

Source docs:

- [`ARCHITECTURE.md`](ARCHITECTURE.md), section 4.12
- [`IMPLEMENTATION_BLUEPRINT.md`](IMPLEMENTATION_BLUEPRINT.md), Phase 2
- [`SCHEMAS_AND_CONTRACTS.md`](SCHEMAS_AND_CONTRACTS.md)

Implementation checks:

- [ ] Add migration table.
- [ ] Add agents tables.
- [ ] Add catalog tables.
- [ ] Add observed entries table.
- [ ] Add remediation plans table.
- [ ] Add approvals table.
- [ ] Add backups table.
- [ ] Add runtime tables.
- [ ] Add credential tables.
- [ ] Add events table.

Acceptance criteria:

- Fresh DB migrates from zero.
- Migration is idempotent.
- Tests can create isolated temp DBs.

Verification steps:

- Run migration tests.
- Inspect schema dump.

Checkpoint:

- required: true
- artifact: schema dump, migration tests, design notes
- review_gate: schema review before models

Git:

- packet: P1 storage
- branch_hint: task/TASK-004-db-migrations
- commit_boundary: one commit for initial schema
- source_hashes: []

### TASK-005: Schema Models And Validation

Description: implement typed models and validators for observed entries, catalog entries, candidates, plans, approvals, audit events, runtime backends, and health payloads.

Priority: 96

Depends on: [TASK-004]

Workflow lane: storage

Source docs:

- [`SCHEMAS_AND_CONTRACTS.md`](SCHEMAS_AND_CONTRACTS.md)
- [`PRD.md`](PRD.md), sections 7, 8, and 11

Implementation checks:

- [ ] Implement observed entry model.
- [ ] Implement catalog entry model.
- [ ] Implement candidate model.
- [ ] Implement remediation plan model.
- [ ] Implement approval model.
- [ ] Implement audit event model.
- [ ] Implement runtime backend model.
- [ ] Implement health model.
- [ ] Add invalid input tests.

Acceptance criteria:

- Required fields are enforced.
- Unknown unsafe states are not silently accepted.
- Serialization round trips are stable.

Verification steps:

- Run schema model tests.
- Run type checks.

Checkpoint:

- required: true
- artifact: model diff, validation test output
- review_gate: schema/model review before adapter work

Git:

- packet: P1 storage
- branch_hint: task/TASK-005-schema-models
- commit_boundary: one commit for schemas and validation
- source_hashes: []

### TASK-006: Audit Event Writer

Description: implement append-only audit/event writing with redaction and hash-chain support.

Priority: 95

Depends on: [TASK-004, TASK-005]

Workflow lane: observability

Source docs:

- [`ARCHITECTURE.md`](ARCHITECTURE.md), sections 4.12 and 8
- [`PRD.md`](PRD.md), sections 8.7, 10.7, and 12.3

Implementation checks:

- [ ] Implement event append.
- [ ] Implement previous hash linkage.
- [ ] Implement secret redaction helper.
- [ ] Add event query by type/agent/plan.
- [ ] Add tests for redaction and tamper detection.

Acceptance criteria:

- Event writes are append-only.
- Secret-like values are redacted.
- Hash chain detects mutation.

Verification steps:

- Run event writer tests.
- Manually inspect emitted sample.

Checkpoint:

- required: true
- artifact: sample event log, test output
- review_gate: security review before credential work

Git:

- packet: P1 observability
- branch_hint: task/TASK-006-audit-events
- commit_boundary: one commit for audit event writer
- source_hashes: []

### TASK-007: Agent Registration Model

Description: implement agent registration records, identity hints, and certification levels.

Priority: 94

Depends on: [TASK-005]

Workflow lane: adapters

Source docs:

- [`ARCHITECTURE.md`](ARCHITECTURE.md), section 4.2
- [`PRD.md`](PRD.md), sections 9 and 11.10

Implementation checks:

- [ ] Add create/list/show agent registration APIs.
- [ ] Store `agent_id`, `agent_kind`, workspace root, config paths, token ref, and certification level.
- [ ] Validate supported agent kinds.
- [ ] Add temp-home tests.

Acceptance criteria:

- Agent records are durable.
- Certification level controls remediation permissions later.
- Invalid agent kind is rejected.

Verification steps:

- Run agent registry tests.
- Create sample agent and inspect stored row.

Checkpoint:

- required: false
- artifact: test output
- review_gate: none

Git:

- packet: P2 adapters
- branch_hint: task/TASK-007-agent-registry
- commit_boundary: one commit for agent registry
- source_hashes: []

### TASK-008: Config Path Discovery

Description: discover known config paths for first-wave agents without parsing or mutating them.

Priority: 93

Depends on: [TASK-007]

Workflow lane: adapters

Source docs:

- [`PRD.md`](PRD.md), sections 9.4 and 11.3
- [`IMPLEMENTATION_BLUEPRINT.md`](IMPLEMENTATION_BLUEPRINT.md), Phase 3

Implementation checks:

- [ ] Implement discovery for Codex.
- [ ] Implement discovery for Claude Code.
- [ ] Implement discovery for Gemini.
- [ ] Implement discovery for Cline.
- [ ] Implement discovery for OpenCode.
- [ ] Add temp-home fixture tests.

Acceptance criteria:

- Discovery returns existing files only.
- Discovery reports missing expected files as notices, not errors.
- No files are created or mutated.

Verification steps:

- Run discovery tests.
- Run CLI discovery in temp home.

Checkpoint:

- required: false
- artifact: test output
- review_gate: none

Git:

- packet: P2 adapters
- branch_hint: task/TASK-008-config-discovery
- commit_boundary: one commit for config discovery
- source_hashes: []

### TASK-009 To TASK-013: First-Wave Adapter Parsers

Description: implement parser and normalization fixture packets for Codex, Claude Code, Gemini CLI, Cline, and OpenCode.

Priority: 92

Depends on: [TASK-008]

Workflow lane: adapters

Source docs:

- [`PRD.md`](PRD.md), sections 9.1 through 9.4
- [`SCHEMAS_AND_CONTRACTS.md`](SCHEMAS_AND_CONTRACTS.md), Normalized Observed MCP Entry
- [`ACCEPTANCE_TEST_PLAN.md`](ACCEPTANCE_TEST_PLAN.md), Agent Config Discovery

Task split:

- TASK-009: Codex parser and fixtures.
- TASK-010: Claude Code parser and fixtures.
- TASK-011: Gemini CLI parser and fixtures.
- TASK-012: Cline parser and fixtures.
- TASK-013: OpenCode parser and fixtures.

Implementation checks per adapter:

- [ ] Add direct stdio fixture.
- [ ] Add Hub-routed HTTP fixture.
- [ ] Add disabled entry fixture.
- [ ] Add env/cwd/args fixture.
- [ ] Add unknown/unsupported field fixture.
- [ ] Normalize to `ObservedMcpEntry`.
- [ ] Preserve config path/container path.

Acceptance criteria:

- Adapter parses all fixtures.
- Normalized output is deterministic.
- Unsupported fields do not disappear.
- No rewrite behavior is implemented in parser packet.

Verification steps:

- Run adapter fixture tests.
- Run schema validation on normalized entries.

Checkpoint:

- required: true
- artifact: fixture corpus, normalized snapshots, test output
- review_gate: adapter review before audit ingestion

Git:

- packet: P2 adapters
- branch_hint: task/TASK-009-013-first-wave-parsers
- commit_boundary: one semantic commit per adapter
- source_hashes: []

### TASK-014: Observed-Entry Ingestion And Health Classification

Description: ingest normalized observed entries and classify compliant Hub-routed entries, direct bypasses, unknown entries, disabled entries, and unsupported entries.

Priority: 88

Depends on: [TASK-009, TASK-010, TASK-011, TASK-012, TASK-013]

Workflow lane: audit

Source docs:

- [`PRD.md`](PRD.md), sections 8.1 and 8.8
- [`ARCHITECTURE.md`](ARCHITECTURE.md), section 7

Implementation checks:

- [ ] Store observed entries.
- [ ] Classify Hub-routed entries.
- [ ] Classify active direct bypasses as blockers.
- [ ] Classify disabled direct entries as warnings/notices.
- [ ] Emit `config.observed` and `config.drift_detected`.

Acceptance criteria:

- Active direct bypass appears as blocker.
- Hub-routed entry appears compliant.
- Disabled unknown entry does not block.

Verification steps:

- Run audit classification tests.
- Inspect health payload fixture.

Checkpoint:

- required: true
- artifact: health samples, event samples, test output
- review_gate: review before watcher/periodic audit

Git:

- packet: P3 audit
- branch_hint: task/TASK-014-observed-ingestion
- commit_boundary: one commit for ingestion and classification
- source_hashes: []

### TASK-015: Watchers And Periodic Audit

Description: implement daemon config watchers with periodic full-audit fallback.

Priority: 86

Depends on: [TASK-014]

Workflow lane: audit

Source docs:

- [`PRD.md`](PRD.md), section 8.1
- [`ARCHITECTURE.md`](ARCHITECTURE.md), section 4.1

Implementation checks:

- [ ] Add watcher abstraction.
- [ ] Add polling fallback.
- [ ] Debounce rapid writes.
- [ ] Re-parse changed config.
- [ ] Emit audit trigger events.

Acceptance criteria:

- File change triggers re-audit.
- Polling fallback works when watcher unavailable.
- Rapid partial writes do not corrupt state.

Verification steps:

- Run watcher tests with temp files.
- Run manual smoke with edited fixture config.

Checkpoint:

- required: true
- artifact: watcher test output, debounce notes
- review_gate: review before auto-planning

Git:

- packet: P3 audit
- branch_hint: task/TASK-015-watchers-audit
- commit_boundary: one commit for watcher and periodic audit
- source_hashes: []

### TASK-016 To TASK-018: Catalog Matching And Candidate Staging

Description: implement catalog storage, required metadata validation, matching, confidence scoring, and candidate staging.

Priority: 84

Depends on: [TASK-014]

Workflow lane: catalog

Source docs:

- [`PRD.md`](PRD.md), sections 7.1, 8.3, and 11.2
- [`SCHEMAS_AND_CONTRACTS.md`](SCHEMAS_AND_CONTRACTS.md), Catalog Entry and Catalog Candidate
- [`ARCHITECTURE.md`](ARCHITECTURE.md), section 4.4

Task split:

- TASK-016: catalog storage and metadata validation.
- TASK-017: fingerprint, alias, URL, and confidence matching.
- TASK-018: unknown stdio and local HTTP candidate staging.

Implementation checks:

- [ ] Validate required metadata before routing.
- [ ] Match exact Hub URL.
- [ ] Match exact command/args fingerprint.
- [ ] Match URL normalization.
- [ ] Match aliases only as non-auto weak confidence unless reinforced.
- [ ] Stage unknown stdio disabled/pending.
- [ ] Stage unknown local HTTP without auto-route.

Acceptance criteria:

- Known direct entry maps to approved catalog entry.
- Unknown stdio creates candidate.
- Unknown local HTTP creates candidate and blocker if active.
- Weak name match cannot auto-apply.

Verification steps:

- Run catalog matching tests.
- Run candidate staging tests.

Checkpoint:

- required: true
- artifact: match matrix, candidate samples, test output
- review_gate: review before remediation planning

Git:

- packet: P4 catalog
- branch_hint: task/TASK-016-018-catalog-matching
- commit_boundary: one commit for storage, one for matching, one for candidates
- source_hashes: []

### TASK-019 To TASK-020: Remediation Planning

Description: implement dry-run remediation plans and self-healing state machine up to `PLAN`.

Priority: 82

Depends on: [TASK-016, TASK-017, TASK-018]

Workflow lane: planning

Source docs:

- [`PRD.md`](PRD.md), sections 8.1, 8.2, 8.3, and 11.4
- [`ARCHITECTURE.md`](ARCHITECTURE.md), section 5
- [`SCHEMAS_AND_CONTRACTS.md`](SCHEMAS_AND_CONTRACTS.md), Remediation Plan

Task split:

- TASK-019: remediation plan model, diff generation, policy decision.
- TASK-020: full self-healing dry-run pipeline from observe to plan.

Implementation checks:

- [ ] Generate known direct rewrite plan.
- [ ] Generate unknown import plan.
- [ ] Generate unsafe local HTTP plan.
- [ ] Generate missing control-plane plan.
- [ ] Include exact file, before/after, policy reason, approval state.
- [ ] Plans are dry-run only.

Acceptance criteria:

- Plans are deterministic.
- Plans cannot mutate files.
- Every plan has verification and rollback expectation.

Verification steps:

- Run planning tests.
- Snapshot example plan payloads.

Checkpoint:

- required: true
- artifact: plan payloads, diff samples, policy matrix, test output
- review_gate: human review before any apply code

Git:

- packet: P5 planning
- branch_hint: task/TASK-019-020-remediation-planning
- commit_boundary: one commit for planner, one for pipeline
- source_hashes: []

### TASK-021 To TASK-023: Approval, Atomic Apply, And Safe Auto-Apply

Description: implement approval lifecycle, atomic writer, backups, verification, rollback, and narrowly scoped auto-apply.

Priority: 80

Depends on: [TASK-019, TASK-020]

Workflow lane: remediation

Source docs:

- [`PRD.md`](PRD.md), sections 8.9, 11.5, 12.1, and 12.2
- [`ARCHITECTURE.md`](ARCHITECTURE.md), sections 4.7 and 4.8
- [`ACCEPTANCE_TEST_PLAN.md`](ACCEPTANCE_TEST_PLAN.md), Known Direct MCP Rewrite and Atomic Rollback

Task split:

- TASK-021: approval lifecycle and CLI approval commands.
- TASK-022: atomic writer, backup, verification, rollback.
- TASK-023: auto-apply for certified safe known-direct rewrites only.

Implementation checks:

- [ ] Approval required plans cannot apply without approval.
- [ ] Backups preserve original bytes.
- [ ] Syntax validation runs before rename.
- [ ] Post-write parse verifies desired state.
- [ ] Failed validation rolls back.
- [ ] Auto-apply rejects uncertified adapters.
- [ ] Rewrite loop guard prevents repeated churn.

Acceptance criteria:

- Approved plan applies atomically.
- Rejected plan cannot apply.
- Rollback restores exact pre-image hash.
- Auto-apply works only for certified safe cases.

Verification steps:

- Run apply tests.
- Run rollback tests.
- Run auto-apply eligibility tests.

Checkpoint:

- required: true
- artifact: before/after configs, backup sample, rollback proof, test output
- review_gate: adversarial review before real config mutation

Git:

- packet: P6 remediation
- branch_hint: task/TASK-021-023-approval-apply
- commit_boundary: one commit for approvals, one for writer/rollback, one for auto-apply gate
- source_hashes: []

### TASK-024 To TASK-029: Runtime Proxy And Isolation

Description: implement MCP data-plane proxy with backend transports, session mapping, sharing policy, crash recovery, and runtime events.

Priority: 76

Depends on: [TASK-005, TASK-006, TASK-016]

Workflow lane: runtime

Source docs:

- [`PRD.md`](PRD.md), sections 8.5, 8.6, and 11.6
- [`ARCHITECTURE.md`](ARCHITECTURE.md), sections 4.9, 4.10, and 6
- [`ACCEPTANCE_TEST_PLAN.md`](ACCEPTANCE_TEST_PLAN.md), Runtime Proxy and Hot Reuse tests

Task split:

- TASK-024: Streamable HTTP frontend route.
- TASK-025: local stdio backend transport.
- TASK-026: remote HTTP backend transport.
- TASK-027: frontend/backend session mapping and request ID rewrite.
- TASK-028: shareability policy, isolation keys, hot reuse metrics.
- TASK-029: crash recovery, idle reaping, cancellation, runtime events.

Implementation checks:

- [ ] Implement `/servers/<server>/mcp`.
- [ ] Generate frontend session IDs.
- [ ] Select backend pool key from catalog policy.
- [ ] Rewrite JSON-RPC request IDs.
- [ ] Cache initialize only by policy.
- [ ] Scope notifications.
- [ ] Serialize mutating calls where policy requires.
- [ ] Emit runtime events.

Acceptance criteria:

- Local stdio fake backend works.
- Remote HTTP fake backend works.
- Shareable server has one backend for two frontend sessions.
- Non-shareable server has separate backends.
- Backend crash produces clear error and lazy restart.

Verification steps:

- Run runtime unit tests.
- Run fake backend integration tests.
- Inspect runtime event payloads.

Checkpoint:

- required: true
- artifact: runtime trace samples, hot reuse proof, isolation proof, test output
- review_gate: runtime review before real-client certification

Git:

- packet: P7 runtime
- branch_hint: task/TASK-024-029-runtime-proxy
- commit_boundary: one semantic commit per runtime subtask
- source_hashes: []

### TASK-030 To TASK-034: Credentials And Security

Description: implement safe credential references/readiness/resolution and local control-plane security controls.

Priority: 72

Depends on: [TASK-005, TASK-006, TASK-024]

Workflow lane: security

Source docs:

- [`PRD.md`](PRD.md), sections 8.7, 12.2, and 12.3
- [`ARCHITECTURE.md`](ARCHITECTURE.md), sections 4.11 and 8
- [`ACCEPTANCE_TEST_PLAN.md`](ACCEPTANCE_TEST_PLAN.md), Credential tests

Task split:

- TASK-030: credential reference schema and readiness states.
- TASK-031: env, `.env`, keychain placeholder, and `pass` no-prompt readiness.
- TASK-032: backend-startup secret resolution with redaction.
- TASK-033: local auth tokens and agent registration tokens.
- TASK-034: Origin, URL, command safety, and security denials.

Implementation checks:

- [ ] Broad status never resolves secret values.
- [ ] Locked `pass` does not prompt.
- [ ] Backend startup resolves only required active secrets.
- [ ] Logs redact secret-like strings.
- [ ] Control-plane mutation requires auth.
- [ ] Browser-origin requests are denied unless explicitly allowed.
- [ ] Dangerous shell-string command imports are rejected or approval-gated.

Acceptance criteria:

- No secret value appears in logs/tests.
- Active missing credential is blocker.
- Dormant missing credential is not blocker.
- Unauthorized mutation request is denied.

Verification steps:

- Run credential tests.
- Run security denial tests.
- Inspect logs for redaction.

Checkpoint:

- required: true
- artifact: security test output, redaction proof, denial samples
- review_gate: security review before exposing apply through surfaces

Git:

- packet: P8 security
- branch_hint: task/TASK-030-034-credentials-security
- commit_boundary: one commit for credentials, one for auth/security controls
- source_hashes: []

### TASK-035 To TASK-037: Operator Surfaces

Description: implement CLI, control-plane MCP, and TUI workflows.

Priority: 68

Depends on: [TASK-020, TASK-022, TASK-024, TASK-030, TASK-033]

Workflow lane: operator

Source docs:

- [`PRD.md`](PRD.md), sections 10, 11.8, 11.9, and 11.10
- [`ARCHITECTURE.md`](ARCHITECTURE.md), section 3.2

Task split:

- TASK-035: CLI compact status, JSON status, audit, plan, approval, apply, rollback, catalog, runtime, release gate.
- TASK-036: `mcp_hub` control-plane MCP self-check/status/plan/proxy/runtime/credential/catalog tools.
- TASK-037: TUI dashboard, problems, approvals, candidates, runtime, credentials, rollback, why-slow, what-changed.

Implementation checks:

- [ ] CLI commands output stable JSON where applicable.
- [ ] Compact status avoids huge raw JSON.
- [ ] Control MCP scopes responses to invoking agent.
- [ ] Control MCP cannot apply destructive actions without approval.
- [ ] TUI shows blockers/warnings/notices separately.
- [ ] TUI can approve plans and show diffs.
- [ ] TUI can show rollback path.

Acceptance criteria:

- Operator can answer "why is my agent slow?"
- Operator can answer "what changed my config?"
- Agent can call `mcp_hub.self_check`.
- Destructive actions remain approval-gated.

Verification steps:

- Run CLI tests.
- Run control MCP protocol tests.
- Run TUI tests or smoke scripts.

Checkpoint:

- required: true
- artifact: CLI samples, MCP tool list, TUI screenshots/logs, test output
- review_gate: UX/security review before real-client certification

Git:

- packet: P9 operator
- branch_hint: task/TASK-035-037-operator-surfaces
- commit_boundary: one commit per surface
- source_hashes: []

### TASK-038 To TASK-042: Real-Client Certification

Description: certify first-wave real clients with Hub-routed MCP entries, self-check, tool call, runtime evidence, and rollback.

Priority: 60

Depends on: [TASK-023, TASK-029, TASK-034, TASK-035, TASK-036]

Workflow lane: acceptance

Source docs:

- [`ACCEPTANCE_TEST_PLAN.md`](ACCEPTANCE_TEST_PLAN.md), Real-Client Acceptance Tests
- [`PRD.md`](PRD.md), sections 9, 17, and 18

Task split:

- TASK-038: Codex CLI certification.
- TASK-039: Claude Code certification.
- TASK-040: Gemini CLI certification.
- TASK-041: Cline certification.
- TASK-042: OpenCode certification.

Implementation checks per client:

- [ ] Install `mcp_hub` control-plane MCP.
- [ ] Add direct known MCP fixture.
- [ ] Detect drift.
- [ ] Rewrite through Hub.
- [ ] Verify client sees Hub-routed MCP.
- [ ] Run safe tool call.
- [ ] Verify runtime event route.
- [ ] Roll back config.

Acceptance criteria:

- Client passes end-to-end certification.
- Adapter certification level can be upgraded to `certified`.
- Automatic remediation can be enabled only after pass.

Verification steps:

- Run real-client certification script.
- Store redacted transcript and audit events.

Checkpoint:

- required: true
- artifact: client transcript, config before/after, runtime events, rollback proof
- review_gate: final human approval per client before certified status

Git:

- packet: P10 certification
- branch_hint: task/TASK-038-042-real-client-certification
- commit_boundary: one commit per client certification harness/result docs
- source_hashes: []

### TASK-043 To TASK-044: Release Gate And Final Review

Description: implement release gate, migration dry-run, adversarial review, and rollback drill.

Priority: 55

Depends on: [TASK-038, TASK-039, TASK-040, TASK-041, TASK-042]

Workflow lane: release

Source docs:

- [`PRD.md`](PRD.md), section 17
- [`ACCEPTANCE_TEST_PLAN.md`](ACCEPTANCE_TEST_PLAN.md), Release Gate
- [`IMPLEMENTATION_BLUEPRINT.md`](IMPLEMENTATION_BLUEPRINT.md), Phase 11

Task split:

- TASK-043: release gate and migration dry-run.
- TASK-044: adversarial review and rollback drill.

Implementation checks:

- [ ] Release gate fails on active direct bypass.
- [ ] Release gate fails on failed real-client certification.
- [ ] Release gate fails if secret value appears in logs.
- [ ] Release gate fails if non-shareable backend is shared.
- [ ] Migration dry-run imports legacy state without mutating live configs.
- [ ] Rollback drill restores configs from backups.

Acceptance criteria:

- Release gate reflects machine-level readiness.
- Migration dry-run is safe.
- Final rollback drill passes.

Verification steps:

- Run full release gate.
- Run migration dry-run.
- Run rollback drill.

Checkpoint:

- required: true
- artifact: release report, migration report, rollback transcript
- review_gate: final human release approval

Git:

- packet: P11 release
- branch_hint: task/TASK-043-044-release-gate
- commit_boundary: one commit for release gate, one commit for final review artifacts
- source_hashes: []

## 5. Dependency Map

```text
TASK-001
  -> TASK-002
  -> TASK-003
  -> TASK-004
  -> TASK-005
  -> TASK-006

TASK-005 -> TASK-007 -> TASK-008 -> TASK-009..TASK-013 -> TASK-014 -> TASK-015

TASK-014 -> TASK-016 -> TASK-017 -> TASK-018 -> TASK-019 -> TASK-020 -> TASK-021..TASK-023

TASK-005 + TASK-006 + TASK-016 -> TASK-024..TASK-029

TASK-005 + TASK-006 + TASK-024 -> TASK-030..TASK-034

TASK-020 + TASK-022 + TASK-024 + TASK-030 + TASK-033 -> TASK-035..TASK-037

TASK-023 + TASK-029 + TASK-034 + TASK-035 + TASK-036 -> TASK-038..TASK-042

TASK-038..TASK-042 -> TASK-043..TASK-044
```

Validation notes:

- No apply work begins before dry-run planning checkpoint.
- No real config mutation begins before atomic apply checkpoint.
- No automatic remediation begins before certified adapter checkpoint.
- No release begins before real-client certification checkpoint.

## 6. Fresh-Session Start Checklist

- [ ] Read [`README.md`](../README.md).
- [ ] Read [`PRD.md`](PRD.md).
- [ ] Read [`ARCHITECTURE.md`](ARCHITECTURE.md).
- [ ] Read [`SCHEMAS_AND_CONTRACTS.md`](SCHEMAS_AND_CONTRACTS.md).
- [ ] Read [`IMPLEMENTATION_BLUEPRINT.md`](IMPLEMENTATION_BLUEPRINT.md).
- [ ] Read [`ACCEPTANCE_TEST_PLAN.md`](ACCEPTANCE_TEST_PLAN.md).
- [ ] Pick the first uncompleted task whose dependencies are complete.
- [ ] Create or use branch from the task `branch_hint`.
- [ ] Implement only that task packet.
- [ ] Run task verification steps.
- [ ] Produce checkpoint artifact if required.
- [ ] Stop for review if `review_gate` is set.
