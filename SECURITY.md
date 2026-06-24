# Security Policy

## Supported Versions

Security fixes are provided for the latest released version of MCP Multiplex.
Pre-release branches and historical development snapshots are supported on a
best-effort basis.

## Reporting A Vulnerability

Do not open a public issue for a suspected vulnerability, credential exposure,
or unsafe configuration mutation.

Use the repository's **Security** tab to submit a private vulnerability report
through GitHub Security Advisories:

<https://github.com/AmeerJ97/mcp-multiplex/security/advisories/new>

Include:

- the affected version or commit;
- the relevant command, daemon route, or agent adapter;
- reproduction steps using redacted fixtures;
- the expected and observed security boundary;
- any evidence of credential disclosure or irreversible mutation.

Never include live access tokens, API keys, private configuration files, or
unredacted audit databases.

The maintainers will acknowledge a complete report as soon as practical,
validate the impact, coordinate a fix and disclosure timeline, and credit the
reporter unless anonymity is requested.

## Security Boundaries

MCP Multiplex treats agent configuration as a projection, requires approval for
destructive or ambiguous changes, stores secret references instead of raw
values, and records mutation and rollback evidence. A bypass of any of these
boundaries should be reported as a security issue.
