# Implementation Blueprint

## 1. Execution Strategy

The rebuild must proceed in phases. Do not begin by building a full proxy, TUI, and migration system simultaneously.

Each phase must produce working software, tests, and operator-visible evidence.

The order is:

1. Stabilize and freeze old behavior.
2. Build daemon state and schemas.
3. Build config adapters and observed-entry normalization.
4. Build read-only audit.
5. Build catalog matching and candidate staging.
6. Build remediation planning.
7. Build approval and atomic apply.
8. Build runtime proxy and isolation.
9. Build credential readiness and resolver.
10. Build operator surfaces.
11. Run real-client acceptance.

## 2. Phase 0: Stop the Bleeding

Objective: create a safe baseline before migration.

Tasks:

- mark old `mcp-hub` as legacy,
- stop adding features to old repo,
- export current config and runtime state,
- document current direct bypasses,
- document current known active agent configs,
- capture current working MCP set,
- define rollback path.

Exit criteria:

- old system can still be used,
- current configs are backed up,
- no rebuild code mutates real configs yet.

## 3. Phase 1: Project Foundation

Objective: create the new project skeleton.

Tasks:

- choose language/runtime,
- create daemon package,
- create CLI package,
- create TUI package placeholder,
- create schema package,
- create test fixture layout,
- configure lint/test/type checks,
- create local development database migration harness.

Recommended initial layout:

```text
mcp-multiplex/
  src/
    daemon/
    cli/
    control_mcp/
    adapters/
    catalog/
    runtime/
    credentials/
    storage/
    approvals/
    observability/
  tui/
  tests/
    fixtures/
      agents/
      catalog/
      runtime/
    acceptance/
  docs/
```

Exit criteria:

- empty daemon starts and exposes health,
- migrations run,
- CLI can call daemon health,
- tests run in CI/local.

## 4. Phase 2: Schemas and Storage

Objective: implement durable state before behavior.

Tasks:

- implement SQLite schema,
- implement declarative config loader,
- implement schema validation,
- implement audit event writer,
- implement hash-chain audit table,
- implement backup metadata table,
- implement migration tests.

Required tables:

- `agents`,
- `agent_config_paths`,
- `catalog_entries`,
- `catalog_aliases`,
- `catalog_provenance`,
- `catalog_candidates`,
- `profiles`,
- `profile_servers`,
- `observed_entries`,
- `remediation_plans`,
- `approvals`,
- `config_backups`,
- `runtime_backends`,
- `runtime_frontend_sessions`,
- `credential_refs`,
- `events`,
- `schema_migrations`.

Exit criteria:

- schema migrates up/down in tests,
- model validation catches invalid catalog entries,
- audit writer redacts secrets.

## 5. Phase 3: Agent Registry and Config Adapters

Objective: parse supported agent configs into normalized observed entries.

First-wave adapters:

- Codex CLI,
- Claude Code,
- Gemini CLI,
- Cline,
- OpenCode.

Tasks per adapter:

- define config paths,
- define parser,
- define serializer,
- define normalization,
- define syntax validation,
- define rewrite capability,
- define reload requirement,
- create fixture tests.

Adapter fixture pattern:

```text
tests/fixtures/agents/codex/
  direct-context7.input.toml
  direct-context7.expected-observed.json
  direct-context7.expected-rewrite.toml
  hub-routed.input.toml
  hub-routed.expected-observed.json
```

Exit criteria:

- each first-wave adapter parses fixture configs,
- observed entries are stable,
- unsupported configs are reported plan-only,
- no real user config is modified.

## 6. Phase 4: Read-Only Audit

Objective: observe and explain without mutation.

Tasks:

- implement config discovery,
- implement file watcher in observe-only mode,
- implement periodic audit,
- implement `mcp-multiplex audit run`,
- implement compact status,
- implement event emission for observed configs,
- implement health computation for read-only state.

Exit criteria:

- daemon can report direct bypasses,
- daemon can report missing control-plane MCP,
- daemon can report unknown entries,
- CLI shows compact health,
- no mutation code path is enabled.

## 7. Phase 5: Catalog Matching and Candidate Staging

Objective: classify observed entries against catalog.

Tasks:

- implement command/args fingerprinting,
- implement URL normalization,
- implement alias matching,
- implement package-name extraction,
- implement candidate creation,
- implement duplicate/variant grouping,
- implement confidence scoring,
- implement candidate review states.

Match confidence levels:

- `exact_hub_url`,
- `exact_install_fingerprint`,
- `canonical_alias`,
- `url_equivalent`,
- `capability_equivalent`,
- `weak_name_match`,
- `unknown`.

Exit criteria:

- known direct `context7` maps to catalog,
- unknown stdio becomes disabled candidate,
- unknown local HTTP becomes staged local HTTP candidate,
- weak matches never auto-apply.

## 8. Phase 6: Remediation Planning

Objective: produce exact safe plans.

Tasks:

- implement remediation plan model,
- implement before/after diff generation,
- implement policy decision engine,
- implement auto-apply eligibility check,
- implement approval task generation,
- expose plans through CLI and control MCP.

Plan types:

- `rewrite_known_direct`,
- `import_unknown_candidate`,
- `route_approved_candidate`,
- `install_missing_control_plane`,
- `remove_duplicate_bypass`,
- `profile_extra_detected`,
- `unsafe_local_http_detected`,
- `unsupported_config_detected`.

Exit criteria:

- all plans are dry-run only,
- plans include exact affected file and diff,
- plans include policy reason,
- plans include approval requirement,
- plans include rollback expectation.

## 9. Phase 7: Approval and Atomic Apply

Objective: safely mutate certified configs.

Tasks:

- implement approval lifecycle,
- implement CLI approve/reject/apply,
- implement atomic writer,
- implement backup store,
- implement syntax validation,
- implement post-apply verification,
- implement rollback.

Auto-apply allowed only when:

- adapter is certified,
- plan is known-safe,
- match confidence is exact or high,
- target config is not project-shared unless policy allows,
- no env/cwd/account ambiguity exists,
- user has not recently overridden the entry,
- rewrite loop guard is clear.

Exit criteria:

- known direct rewrite works in temp fixture,
- backup and rollback restore exact bytes,
- failed validation rolls back automatically,
- approval-gated plan cannot apply without approval.

## 10. Phase 8: Runtime Proxy

Objective: route MCP traffic through per-server Hub endpoints.

Tasks:

- implement `/servers/<server>/mcp`,
- implement frontend session creation,
- implement backend pool key selection,
- implement stdio backend transport,
- implement remote HTTP backend transport,
- implement initialize caching by policy,
- implement request ID rewrite,
- implement notification scoping,
- implement idle reaping,
- implement crash recovery.

Exit criteria:

- fake stdio MCP works,
- fake remote HTTP MCP works,
- hot reuse works for shareable server,
- non-shareable server gets isolated backend,
- runtime events emitted.

## 11. Phase 9: Credentials

Objective: implement safe readiness and startup resolution.

Tasks:

- implement credential ref schema,
- implement env readiness,
- implement `.env` readiness,
- implement keychain readiness,
- implement `pass` metadata readiness without prompt,
- implement backend-startup resolution,
- redact logs,
- expose credential state.

Exit criteria:

- broad status never prints secret values,
- readiness checks do not trigger passphrase prompt,
- active missing credentials are blockers,
- dormant missing credentials are not blockers.

## 12. Phase 10: Operator Surfaces

Objective: make the system usable.

CLI:

- `status --compact`,
- `status --json`,
- `audit run`,
- `plan list`,
- `plan show`,
- `apply <plan-id>`,
- `rollback <audit-id>`,
- `catalog candidates`,
- `runtime ps`,
- `runtime why-slow`,
- `doctor release-gate`.

TUI:

- dashboard,
- problems,
- approvals,
- catalog candidates,
- runtime,
- credentials,
- what changed,
- why slow,
- rollback browser.

Control-plane MCP:

- `self_check`,
- `status`,
- `plan_list`,
- `plan_show`,
- `proxy_url`,
- `runtime_status`,
- `credential_status`.

Exit criteria:

- operator can approve a rewrite from CLI,
- operator can see why an agent is unhealthy,
- control MCP cannot perform destructive actions without prior policy/approval.

## 13. Phase 11: Real-Client Certification

Objective: prove the rebuild works with real clients.

Certification clients:

- Codex CLI,
- Claude Code,
- Gemini CLI,
- Cline,
- OpenCode.

Certification per client:

- install `mcp_hub`,
- install one known direct MCP,
- observe drift,
- rewrite through Hub,
- verify client sees Hub-routed MCP,
- run one tool call,
- verify runtime events,
- verify rollback.

Exit criteria:

- first-wave clients pass acceptance,
- automatic remediation enabled only for passing clients.

