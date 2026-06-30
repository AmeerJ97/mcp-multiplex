# Contributing To MCP Multiplex

MCP Multiplex accepts focused bug fixes, protocol compatibility improvements,
documentation corrections, tests, and carefully scoped features.

## Development Setup

Prerequisites:

- Python 3.12 or newer;
- [uv](https://docs.astral.sh/uv/);
- Linux for user-systemd integration tests and live daemon workflows.

```bash
git clone https://github.com/AmeerJ97/mcp-multiplex.git
cd mcp-multiplex
uv sync --locked --dev
uv run mxp --help
```

Do not run apply, rollback, cutover, or service installation commands against
real user configuration while developing. Use temporary directories and the
fixtures under `tests/`.

## Quality Checks

Run the complete local gate before opening a pull request:

```bash
make check
make build
```

The equivalent commands are:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest -q --tb=short
uv build
```

Use `make format` to apply Ruff formatting.

## Change Expectations

- Preserve named per-server MCP entries and the
  `/servers/<server>/mcp` data-plane contract.
- Keep `mcp_hub` a control-plane MCP, not an omnibus data-plane server.
- Never commit raw secrets, tokens, private agent configs, audit databases, or
  credential-bearing logs.
- Keep config mutations backed up, auditable, verified, and rollback-capable.
- Add tests proportional to the behavior and failure modes changed.
- Update operator documentation when CLI output or policy changes.
- Use official MCP specifications and client documentation for protocol claims.

## Pull Requests

Keep pull requests reviewable and explain:

- the operator-visible behavior;
- the security and rollback implications;
- tests added or changed;
- manual verification performed;
- any compatibility or migration impact.

Use short-lived branches and squash merges. `main` is treated as releasable, so
all pull requests should pass CI before merge. Release-specific steps are
documented in [`docs/RELEASE.md`](docs/RELEASE.md).

By submitting a contribution, you agree that it is licensed under the
[Apache License 2.0](LICENSE).
