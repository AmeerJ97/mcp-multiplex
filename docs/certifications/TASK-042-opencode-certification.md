# OpenCode Certification

Result: PASS
Certification date: 2026-06-22
Client: OpenCode
Client version: `1.17.9`

## Verified

- Installed governed global control-plane configuration without persisting a
  raw bearer token.
- Detected a disposable project-scoped direct MCP entry as an active bypass.
- Rewrote the approved entry while preserving its enabled state.
- Confirmed the real OpenCode CLI merged global `mcp_hub` configuration with
  the governed project server.
- Completed a safe MCP initialize and tool-call round trip through Governor.
- Observed runtime audit events and restored exact pre-change project bytes.

This public summary is redacted. Machine paths, temporary identifiers,
credentials, and detailed runtime transcripts are intentionally excluded.
