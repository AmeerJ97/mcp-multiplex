# Acceptance Tests

This directory is reserved for live-client acceptance tests that exercise MCP
Governor against real coding-agent executables (Codex, Claude Code, Gemini CLI,
Cline, OpenCode) on a disposable machine.

The automated regression suite that runs in CI lives alongside the rest of the
test suite under `tests/` (for example `tests/test_control_plane_install.py`,
`tests/test_runtime_frontend.py`, and `tests/test_release_gate.py`). Those tests
use fixtures under `tests/fixtures/` and prove protocol behavior, config
transformation, and the release gate without requiring real client binaries.

Live-client acceptance is machine- and client-version-specific. The public,
release-gated evidence summaries live under
[`docs/certifications/`](../../docs/certifications/README.md). Run the
machine-level gate before enabling automatic remediation:

```bash
mxp agents self-check --home "$HOME"
mxp doctor release-gate --global-cutover --home "$HOME"
```

See [`docs/ACCEPTANCE_TEST_PLAN.md`](../../docs/ACCEPTANCE_TEST_PLAN.md) for the
full acceptance test plan.
