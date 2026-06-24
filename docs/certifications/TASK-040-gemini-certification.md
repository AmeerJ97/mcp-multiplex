# Gemini CLI Certification

Result: PASS
Certification date: 2026-06-22
Client: Gemini CLI
Client version: `0.47.0`

## Verified

- Installed governed control-plane configuration without persisting a raw
  bearer token.
- Detected a disposable direct MCP entry as an active bypass.
- Rewrote the approved entry using Gemini's HTTP URL representation.
- Confirmed the real Gemini CLI listed `mcp_hub` and the governed server.
- Completed a safe MCP initialize and tool-call round trip through Governor.
- Observed runtime audit events and restored exact pre-change config bytes.

This public summary is redacted. Machine paths, temporary identifiers,
credentials, and detailed runtime transcripts are intentionally excluded.
