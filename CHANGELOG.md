# Changelog

All notable changes to MCP Multiplex are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Split CI workflow with explicit lint, type-check, test, and package jobs.
- Tag-based release workflow for GitHub Releases and optional PyPI Trusted
  Publishing.
- CodeQL and OpenSSF Scorecard workflows.
- Release process, support, and code owner documentation.

### Fixed

- CI now isolates `mxp config inspect --home` from ambient runner XDG paths.
- OpenSSF Scorecard now uploads SARIF to GitHub without relying on
  scorecard.dev publishing.

## [0.1.0] - 2026-06-22

### Added

- Daemon-first local control plane for governed Model Context Protocol server
  routing across coding agents.
- Named per-server MCP routing through
  `http://127.0.0.1:30000/servers/<server>/mcp`.
- Authenticated `mcp_hub` control-plane MCP with agent-scoped read-only tools.
- Config discovery and adapters for Codex, Claude Code, Gemini CLI, Cline, and
  OpenCode.
- Catalog matching, candidate staging, direct-bypass detection, and
  approval-gated remediation planning.
- Atomic config updates with byte-preserving backups, post-write verification,
  audit events, and rollback.
- On-demand stdio and Streamable HTTP backends with policy-controlled session
  sharing and isolation.
- Secret-reference storage and runtime credential resolution without durable
  raw-token storage.
- `mxp` CLI, operator REPL, user-systemd installation, MCP Hub migration
  dry-runs, release gates, and retirement checks.
- Redacted, release-gated real-client certification evidence for all five
  supported client adapters.
- Apache-2.0 licensing, `SECURITY.md`, `CONTRIBUTING.md`, issue templates, pull
  request template, Dependabot configuration, and CI matrix for Python 3.12 and
  3.13.

### Security

- Loopback binding by default.
- Browser `Origin` header validation.
- Control-plane authorization separated from data-plane routing.
- Agent-scoped control tokens with permission scopes.
- Credential references instead of raw values.
- Redacted audit payloads and rejection of credential-bearing remote URLs.
- Approval required for destructive or uncertain actions.
- Pre-image bytes and hashes preserved for rollback.

[Unreleased]: https://github.com/AmeerJ97/mcp-multiplex/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AmeerJ97/mcp-multiplex/releases/tag/v0.1.0
