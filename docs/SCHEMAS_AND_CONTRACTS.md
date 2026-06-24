# Schemas and Contracts

## 1. URL Contracts

### Data Plane

```text
http://127.0.0.1:30000/servers/<server>/mcp
```

### Health

```text
GET http://127.0.0.1:30000/healthz
```

### Runtime Admin

Runtime admin endpoints must require local control-plane authentication.

## 2. Normalized Observed MCP Entry

```json
{
  "schema_version": 1,
  "observed_entry_id": "obs_...",
  "agent_id": "agent_codex_user_default",
  "agent_kind": "codex",
  "config_path": "~/.codex/config.toml",
  "container_path": ["mcp_servers", "context7"],
  "mount_name": "context7",
  "enabled": true,
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@upstash/context7-mcp"],
  "url": null,
  "headers_present": [],
  "env_names": [],
  "cwd": null,
  "tool_filters": {
    "enabled_tools": null,
    "disabled_tools": []
  },
  "approval_policy": null,
  "entry_hash": "sha256:...",
  "raw_shape": "codex-toml",
  "parser_confidence": "complete"
}
```

## 3. Catalog Entry

```json
{
  "schema_version": 1,
  "catalog_id": "srv_...",
  "name": "context7",
  "canonical_name": "upstash.context7",
  "family_id": "context7",
  "variant_name": "official_npm",
  "display_label": "Context7",
  "aliases": ["context7-mcp", "@upstash/context7-mcp"],
  "review_state": "approved",
  "lifecycle_state": "enabled",
  "risk_tier": "normal",
  "provenance": [],
  "transport": {
    "frontend": "streamable_http",
    "hub_path": "/servers/context7/mcp",
    "backend": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp"],
      "cwd_policy": "none",
      "env": []
    }
  },
  "runtime": {
    "shareability": "global",
    "concurrency": "concurrent_readonly",
    "idle_timeout_sec": 600,
    "health_check": "tools_list"
  },
  "credentials": [],
  "active_set": {
    "eligible_profiles": ["coding-default", "docs"],
    "default_enabled": false
  }
}
```

## 4. Catalog Candidate

```json
{
  "schema_version": 1,
  "candidate_id": "cand_...",
  "source": "observed_agent_config",
  "observed_entry_id": "obs_...",
  "proposed_name": "new-server",
  "classification": "unknown_stdio",
  "review_state": "pending",
  "risk_tier": "unknown",
  "confidence": "low",
  "backend_shape": {
    "type": "stdio",
    "command": "uvx",
    "args": ["some-mcp-server"]
  },
  "approval_required": true,
  "reasons": ["unknown_package", "not_in_catalog"]
}
```

## 5. Remediation Plan

```json
{
  "schema_version": 1,
  "plan_id": "plan_...",
  "plan_type": "rewrite_known_direct",
  "status": "pending_approval",
  "agent_id": "agent_codex_user_default",
  "target_path": "~/.codex/config.toml",
  "observed_entry_id": "obs_...",
  "catalog_id": "srv_...",
  "policy": {
    "auto_apply_allowed": false,
    "approval_required": true,
    "approval_reason": "project_shared_config"
  },
  "diff": {
    "format": "unified",
    "text": "--- before\n+++ after\n..."
  },
  "expected_preimage_hash": "sha256:...",
  "rollback_strategy": "restore_backup",
  "risk": {
    "tier": "normal",
    "reasons": []
  },
  "created_at": "2026-06-20T00:00:00Z"
}
```

## 6. Approval

```json
{
  "schema_version": 1,
  "approval_id": "appr_...",
  "plan_id": "plan_...",
  "state": "approved",
  "actor": "local_operator",
  "channel": "tui",
  "created_at": "2026-06-20T00:00:00Z",
  "expires_at": "2026-06-20T01:00:00Z",
  "decision_at": "2026-06-20T00:05:00Z",
  "comment": "Approved known rewrite"
}
```

## 7. Audit Event

```json
{
  "schema_version": 1,
  "event_id": "evt_...",
  "event_type": "remediation.applied",
  "actor": "daemon",
  "agent_id": "agent_codex_user_default",
  "plan_id": "plan_...",
  "target_path": "~/.codex/config.toml",
  "before_hash": "sha256:...",
  "after_hash": "sha256:...",
  "backup_id": "bak_...",
  "result": "success",
  "timestamp": "2026-06-20T00:00:00Z",
  "redaction": "secret_values_removed",
  "previous_event_hash": "sha256:...",
  "event_hash": "sha256:..."
}
```

## 8. Runtime Backend

```json
{
  "schema_version": 1,
  "backend_id": "be_...",
  "catalog_id": "srv_...",
  "runtime_pool_key": "global:context7",
  "state": "hot",
  "pid": 12345,
  "account_scope": "none",
  "workspace_root": null,
  "backend_initialize_count": 1,
  "frontend_session_count": 2,
  "created_at": "2026-06-20T00:00:00Z",
  "last_used_at": "2026-06-20T00:05:00Z"
}
```

## 9. Health Payload

```json
{
  "schema_version": 1,
  "kind": "MCPMultiplexHealth",
  "ok": false,
  "summary": {
    "agents": 5,
    "blockers": 1,
    "warnings": 2,
    "notices": 3,
    "active_servers": 12,
    "hot_backends": 4,
    "pending_approvals": 2
  },
  "blockers": [
    {
      "area": "compliance",
      "code": "active_direct_bypass",
      "agent_id": "agent_codex_user_default",
      "server": "context7",
      "detail": "Codex has an active direct stdio entry for context7"
    }
  ],
  "warnings": [],
  "notices": []
}
```

