# Codex CLI Certification

Result: PASS
Certification date: 2026-06-22
Client: Codex CLI
Client version: `codex-cli 0.141.0`

## Verified

- Installed authenticated `mcp_hub` control-plane configuration without
  persisting a raw bearer token.
- Detected a disposable direct MCP entry as an active bypass.
- Rewrote the approved entry to its named Governor data-plane URL.
- Confirmed the real Codex CLI listed both `mcp_hub` and the governed server.
- Called authenticated `mcp_hub.self_check`.
- Completed a safe MCP initialize and tool-call round trip through Governor.
- Observed runtime audit events and restored exact pre-change config bytes.

This public summary is redacted. Machine paths, temporary identifiers,
credentials, and detailed runtime transcripts are intentionally excluded.
