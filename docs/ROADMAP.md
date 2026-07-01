# Public Roadmap

MCP Multiplex is an Apache-2.0 open core project for local Model Context
Protocol governance. The public roadmap focuses on making the single-user local
control plane reliable, auditable, and easy to adopt.

## Open Source Core

- Stable local daemon and loopback runtime proxy.
- Safe config discovery, planning, approvals, apply, and rollback.
- Public catalog schema and common server metadata.
- Agent adapters for widely used coding agents.
- Read-only `mcp_hub` control-plane tools.
- Security hardening for local auth, origin checks, secret references, and
  audit redaction.
- Release gates, test fixtures, and public compatibility evidence.

## Future Commercial Extensions

Commercial features may be developed separately for teams and organizations:

- Fleet management across multiple developer machines.
- Centralized policy and catalog distribution.
- Organization compliance reports and audit export.
- SSO, RBAC, and enterprise identity integration.
- Managed hosted control plane and administration UI.
- Enterprise secret-manager integrations.
- Signed policy bundles and supply-chain verification.
- Premium support, migration help, and compatibility certification.

The boundary is intentional: single-user local governance stays useful and open;
multi-user organizational governance may become paid infrastructure.
