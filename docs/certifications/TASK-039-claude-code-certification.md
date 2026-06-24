# Claude Code Certification

Result: PASS
Certification date: 2026-06-22
Client: Claude Code
Client version: `2.1.185`

## Verified

- Installed authenticated `mcp_hub` control-plane configuration without
  persisting a raw bearer token.
- Confirmed project-scoped MCP approval behavior.
- Detected a disposable direct MCP entry as an active bypass.
- Rewrote the approved entry to its named Governor data-plane URL.
- Confirmed the real Claude Code client listed `mcp_hub` and the governed
  server.
- Called authenticated `mcp_hub.self_check`.
- Completed a safe MCP initialize and tool-call round trip through Governor.
- Observed runtime audit events and restored exact pre-change config bytes.

This public summary is redacted. Machine paths, temporary identifiers,
credentials, and detailed runtime transcripts are intentionally excluded.
