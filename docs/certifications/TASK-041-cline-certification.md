# Cline Certification

Result: PASS
Certification date: 2026-06-22
Client: Cline CLI
Client version: `3.0.29`

## Verified

- Installed governed control-plane configuration without persisting a raw
  bearer token.
- Detected a disposable direct MCP entry as an active bypass.
- Rewrote the approved entry while preserving Cline-specific fields.
- Confirmed the real Cline CLI reported `mcp_hub` and the governed server.
- Completed a safe MCP initialize and tool-call round trip through Governor.
- Observed runtime audit events and restored exact pre-change config bytes.

This public summary is redacted. Machine paths, temporary identifiers,
credentials, and detailed runtime transcripts are intentionally excluded.
