# Acceptance Test Plan

## 1. Purpose

This document defines the acceptance tests that determine whether MCP Multiplex works end to end.

Fake backend tests are necessary but not sufficient. Release confidence requires real supported clients.

## 2. Test Categories

- schema tests,
- adapter fixture tests,
- daemon audit tests,
- remediation planning tests,
- atomic apply and rollback tests,
- runtime proxy tests,
- credential tests,
- operator UX tests,
- real-client acceptance tests,
- migration tests.

## 3. Core Acceptance Tests

### 3.1 Fresh Bootstrap

Setup:

- clean temporary home,
- empty MCP Multiplex state,
- fixture clients for Codex, Claude Code, Gemini, Cline, OpenCode.

Action:

```text
mcp-multiplex bootstrap --agents codex,claude-code,gemini,cline,opencode
```

Expected:

- daemon config created,
- each supported agent receives `mcp_hub` control-plane MCP,
- default profile entries are Hub-routed,
- audit event `bootstrap.completed`,
- health has no blockers.

### 3.2 Agent Config Discovery

Setup:

- create fixture configs with mixed direct, Hub-routed, disabled, and unknown entries.

Action:

```text
mcp-multiplex audit run --all --json
```

Expected:

- all configs parsed,
- observed entries normalized,
- direct bypasses detected,
- no files mutated.

### 3.3 Known Direct MCP Rewrite

Setup:

- catalog contains approved `context7`,
- Codex config contains direct stdio `context7`.

Action:

```text
mcp-multiplex plan remediate --agent codex
mcp-multiplex apply <plan-id>
```

Expected:

- plan type `rewrite_known_direct`,
- before/after diff shown,
- backup created,
- config rewritten to Hub URL,
- backend command preserved in catalog/provenance,
- verification passes,
- audit event `remediation.applied`.

### 3.4 Unknown Direct MCP Import Planning

Setup:

- agent config contains unknown stdio MCP.

Action:

```text
mcp-multiplex audit run --agent codex
```

Expected:

- catalog candidate created,
- candidate disabled/pending,
- config unchanged,
- health shows warning or blocker depending active direct status,
- TUI approval task available.

### 3.5 Unknown Local HTTP Candidate

Setup:

- agent config contains `http://127.0.0.1:4567/mcp`.

Action:

```text
mcp-multiplex audit run --agent claude-code
```

Expected:

- candidate classified `unknown_local_http`,
- no auto-route,
- process owner probe attempted only if safe,
- approval required,
- active direct bypass remains blocker until resolved.

### 3.6 Approval-Gated Import

Setup:

- unknown candidate exists,
- operator approves import.

Action:

```text
mcp-multiplex catalog approve <candidate-id>
mcp-multiplex apply <plan-id>
```

Expected:

- catalog entry created,
- required metadata complete,
- config routed through Hub if policy allows,
- audit records approval actor.

### 3.7 Atomic Rollback

Setup:

- planned rewrite targets config,
- validation failure injected.

Action:

```text
mcp-multiplex apply <plan-id>
```

Expected:

- write fails safely,
- original bytes restored,
- rollback audit event emitted,
- health reports failed plan with rollback complete.

### 3.8 Runtime Proxy Local Stdio

Setup:

- test stdio MCP catalog entry,
- agent config Hub-routed.

Action:

- initialize through `/servers/test/mcp`,
- call `tools/list`,
- call safe test tool.

Expected:

- backend starts lazily,
- initialize succeeds,
- request IDs are mapped,
- runtime events emitted.

### 3.9 Runtime Proxy Remote HTTP

Setup:

- fake remote HTTP MCP provider,
- catalog entry points to provider,
- agent config Hub-routed.

Action:

- initialize through Hub,
- call tool.

Expected:

- Hub owns frontend session,
- backend remote session behavior follows provider policy,
- response returned,
- runtime events emitted.

### 3.10 Hot Reuse

Setup:

- stateless server marked `global` shareable.

Action:

- two frontend sessions initialize same server.

Expected:

- one backend session,
- two frontend sessions,
- `runtime.backend_reused` event,
- backend initialize count remains one.

### 3.11 Non-Shareable Isolation

Setup:

- browser server marked per workspace or per agent.

Action:

- two agents initialize same server with different isolation keys.

Expected:

- separate backend sessions,
- runtime status explains isolation key,
- no shared browser state.

### 3.12 Credential Readiness Without Resolution

Setup:

- catalog entry references locked `pass` secret.

Action:

```text
mcp-multiplex status --json
```

Expected:

- no passphrase prompt,
- secret value never logged,
- readiness state is non-value status.

### 3.13 Credential Resolution At Startup

Setup:

- active server requires env secret,
- secret source available.

Action:

- start backend through Hub.

Expected:

- secret resolved only at startup,
- value passed to backend env,
- logs redacted.

### 3.14 Unmanaged Process Detection

Setup:

- start direct MCP-like process outside Hub.

Action:

```text
mcp-multiplex audit processes
```

Expected:

- process detected,
- no automatic kill,
- approval task available with process tree.

### 3.15 TUI Truthfulness

Setup:

- seed blockers, warnings, notices, approvals.

Action:

```text
mcp-multiplex tui
```

Expected:

- dashboard groups states correctly,
- approvals visible,
- no raw JSON required for triage,
- operator can inspect diff and rollback.

### 3.16 Control-Plane Self-Check

Setup:

- agent has `mcp_hub` installed with registration env/token.

Action:

- call `mcp_hub.self_check`.

Expected:

- response scoped to invoking agent,
- compliance state returned,
- plan IDs returned,
- no destructive action without approval.

## 4. Real-Client Acceptance Tests

### 4.1 Real Codex

Expected:

- Codex config contains Hub-routed active MCPs,
- `mcp_hub` is available,
- known direct MCP is rewritten,
- Codex can call a Hub-routed test tool,
- runtime events prove route through Hub.

### 4.2 Real Claude Code

Expected:

- Claude Code sees Hub-routed MCP,
- project approval behavior is respected,
- `mcp_hub.self_check` returns agent-scoped status,
- tool call succeeds.

### 4.3 Real Gemini CLI

Expected:

- Gemini config uses `httpUrl`,
- generated aliases avoid known Gemini name pitfalls,
- Gemini discovers tools,
- tool call succeeds through Hub.

### 4.4 Real Cline

Expected:

- Cline config uses Hub remote URL,
- disabled/autoApprove fields preserved,
- tool call succeeds.

### 4.5 Real OpenCode

Expected:

- OpenCode remote MCP entry points to Hub,
- merge precedence respected,
- enabled state preserved,
- tool call succeeds.

## 5. Release Gate

Release is blocked if:

- any first-wave real-client test fails,
- active direct bypass remains,
- atomic rollback fails,
- credential status leaks a value,
- unmanaged process cleanup runs without approval,
- non-shareable backend is shared,
- CLI JSON schema breaks,
- TUI cannot show blocker details,
- control-plane MCP can perform destructive action without approval.

