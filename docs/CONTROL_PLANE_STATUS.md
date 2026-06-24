# Control-Plane Protocol Status

## Scope

`mcp_hub` is MCP Multiplex's agent-facing control-plane MCP. It is intentionally
separate from the named per-server data plane and does not re-export downstream
MCP tools.

Endpoint:

```text
http://127.0.0.1:30000/servers/mcp_hub/mcp
```

## Implemented Streamable HTTP Behavior

The daemon currently implements:

- JSON-RPC `initialize`;
- `Mcp-Session-Id` issuance and subsequent session validation;
- `notifications/initialized` and other notification acceptance with HTTP
  `202 Accepted` and no body;
- `MCP-Protocol-Version` consistency checks when clients supply the header;
- explicit negotiation for protocol versions `2025-03-26`, `2025-06-18`, and
  `2025-11-25`, with `2025-06-18` retained as the no-version compatibility
  default;
- authenticated `tools/list`;
- authenticated `tools/call`;
- HTTP `DELETE` session termination;
- browser `Origin` validation;
- loopback binding by default.

The implementation returns JSON responses for requests and does not currently
open server-initiated SSE streams. The MCP Streamable HTTP specification allows
an implementation that does not offer a standalone SSE stream to return
`405 Method Not Allowed` for GET.

Protocol behavior is covered by `tests/test_runtime_frontend.py`, including
authentication failures, lifecycle notifications, protocol-version mismatch,
tool discovery, tool calls, session scope, and deletion.

## Tool Surface

The control MCP exposes read-only, agent-scoped tools:

- `self_check`
- `status`
- `plan_list`
- `plan_show`
- `proxy_url`
- `runtime_status`
- `credential_status`
- `catalog_search`

Mutation commands are deliberately excluded. Apply and rollback remain
operator workflows with approval, backup, audit, verification, and rollback
requirements.

## Authentication

Tool discovery and invocation require an agent-scoped token with
`control:read`. Initialization and lifecycle notifications do not reveal
governed state and can complete before authenticated tool use.

Supported clients reference token-bearing headers without persisting the raw
token in their durable MCP configuration:

| Client | Strategy |
| --- | --- |
| Codex | `bearer_token_env_var` |
| Claude Code | `headersHelper` |
| Gemini CLI | Environment-backed header template |
| Cline | `mcp-remote` helper reading the environment |
| OpenCode | `{env:...}` header expansion |

Run this after installation:

```bash
mxp agents self-check --home "$HOME"
```

## Certification Boundary

Repository tests prove protocol behavior and config transformation against
fixtures. Real-client certification is machine- and client-version-specific.
The release gate consumes the redacted public evidence summaries in
[`docs/certifications`](certifications/README.md); detailed machine-local
transcripts and audit identifiers remain private.
Before enabling global automatic remediation, run:

```bash
mxp agents self-check --home "$HOME"
mxp doctor release-gate --global-cutover --home "$HOME"
```

The release gate must fail when a target client is not certified, the
authenticated `mcp_hub` projection is missing, a direct bypass is active, the
daemon service is unavailable, or imported legacy catalog state is not ready.

## Specification References

- [MCP architecture](https://modelcontextprotocol.io/docs/learn/architecture)
- [MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)
- [MCP Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
