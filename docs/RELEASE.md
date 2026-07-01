# Release Process

MCP Multiplex uses SemVer while it is pre-1.0:

- patch releases for bug fixes and documentation corrections;
- minor releases for new public CLI, protocol, or package behavior;
- breaking behavior can still change before 1.0, but should be called out in
  the changelog.

## Prerequisites

- `main` is green in CI.
- `CHANGELOG.md` has a dated release section.
- `pyproject.toml` version matches the release tag without the `v` prefix.
- GitHub repository variable `PYPI_PUBLISH_ENABLED` is set to `true` only after
  PyPI Trusted Publishing is configured for the `pypi` environment.

## Release Steps

1. Create a release branch:

   ```bash
   git switch -c release/v0.1.1
   ```

2. Update `pyproject.toml` and `CHANGELOG.md`.

3. Run the local release gate:

   ```bash
   make check
   make build
   uv run twine check dist/*
   ```

4. Open a release PR and squash merge it after CI passes.

5. Tag the release from `main`:

   ```bash
   git switch main
   git pull --ff-only origin main
   git tag -a v0.1.1 -m "MCP Multiplex v0.1.1"
   git push origin v0.1.1
   ```

6. The release workflow builds artifacts, creates the GitHub Release, and
   publishes to PyPI only when `PYPI_PUBLISH_ENABLED=true`.

7. Verify installation from the published source:

   ```bash
   uv tool install mcp-multiplex
   mxp --version
   mxp --help
   ```

## Rollback

If packaging fails before publication, delete the failed GitHub Release and tag,
fix the release PR, and retag. If a broken package is published to PyPI, do not
delete and reuse the version. Publish a new patch version with the fix.
