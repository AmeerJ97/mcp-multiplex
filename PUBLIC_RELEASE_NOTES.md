# MCP Multiplex 0.1.0

MCP Multiplex is the first public release of a daemon-first local governance and
convergence layer for the Model Context Protocol.

## Highlights

- Named per-server MCP routing through
  `http://127.0.0.1:30000/servers/<server>/mcp`.
- Authenticated `mcp_hub` control-plane MCP with agent-scoped read-only tools.
- Config discovery and adapters for Codex, Claude Code, Gemini CLI, Cline, and
  OpenCode.
- Redacted, release-gated real-client certification evidence for all five
  supported client adapters.
- Catalog matching, candidate staging, direct-bypass detection, and
  approval-gated remediation planning.
- Atomic config updates with byte-preserving backups, post-write verification,
  audit events, and rollback.
- On-demand stdio and Streamable HTTP backends with policy-controlled session
  sharing and isolation.
- Secret-reference storage and runtime credential resolution without durable
  raw-token storage.
- `mxp` CLI, branded operator REPL, user-systemd installation, migration
  dry-runs, release gates, and MCP Hub retirement checks.

## Compatibility

- Python 3.12 and 3.13 are covered by CI.
- Linux is the primary supported operating system for daemon user-service and
  process-management workflows.
- The control plane negotiates the 2025-03-26, 2025-06-18, and 2025-11-25
  protocol versions, retains 2025-06-18 as its compatibility default, and
  implements current bodyless notification acknowledgement and
  session-version consistency requirements documented in the official
  [MCP specification](https://modelcontextprotocol.io/specification/2025-11-25).

## Safety

MCP Multiplex does not silently kill unmanaged processes, persist raw bearer
tokens in supported client configs, or automatically apply destructive and
ambiguous remediations. Review `SECURITY.md`, `docs/ARCHITECTURE.md`, and
`docs/MXP_CLI.md` before enabling mutation workflows.

## Release Verification

- Ruff formatting and lint checks pass.
- Mypy passes across `src` and `tests`.
- All 334 tests pass on Python 3.12 and Python 3.13.
- Wheel and source distributions build successfully and pass `twine check`.
- The public release tree passes the complete quality gate without internal
  release artifacts.
- The live daemon, five client self-checks, global release gate, and MCP Hub
  retirement gate pass using hash-bound public certification evidence.
