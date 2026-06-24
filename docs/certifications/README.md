# Real-Client Certification Evidence

These summaries are the public, release-gated evidence for MCP Multiplex's
supported coding-agent adapters. They intentionally omit machine paths,
disposable config contents, raw runtime transcripts, audit identifiers, and
credentials.

Each certification used the real client executable named in the summary, not
only a simulated client. The detailed private transcript verified:

- authenticated `mcp_hub` control-plane installation where supported;
- detection of a direct MCP bypass;
- approval-gated rewrite to the named Governor data-plane URL;
- visibility of the governed entry from the real client;
- a safe MCP initialize and tool-call round trip;
- runtime audit events; and
- byte-exact rollback of the disposable client configuration.

`mxp doctor release-gate` requires all expected summaries to contain
`Result: PASS`. Global cutover additionally requires their current file hashes
to have been imported into the local audit chain:

```bash
mxp certify import-evidence
mxp doctor release-gate --global-cutover
```

Certification is client-version-specific. Re-run the relevant certification
and replace its summary after a material adapter change or client release.
