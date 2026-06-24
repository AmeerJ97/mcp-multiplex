# Public Repository Publication

This private development repository contains internal checkpoint history. Do
not push its existing branches directly to a public remote.

The public repository URL used by package metadata is currently:

```text
https://github.com/AmeerJ97/mcp-multiplex
```

Change the URL in `pyproject.toml`, `README.md`, `SECURITY.md`,
`CONTRIBUTING.md`, and `.github/ISSUE_TEMPLATE/config.yml` before publication if
the final owner or organization differs.

## Recommended Strategy: Clean Export Repository

This avoids rewriting the private development repository and produces one
auditable public root commit.

```bash
# Run from the parent directory of the private repository.
test ! -e ./mcp-multiplex-public
mkdir ./mcp-multiplex-public
rsync -a ./mcp-multiplex/ ./mcp-multiplex-public/ \
  --exclude .git/ \
  --exclude .venv/ \
  --exclude .pytest_cache/ \
  --exclude .mypy_cache/ \
  --exclude .ruff_cache/ \
  --exclude dist/ \
  --exclude .codex-checkpoints/ \
  --exclude docs/checkpoints/ \
  --exclude docs/FRESH_SESSION_PROMPT.md \
  --exclude docs/GITHUB_POLISH_MASTER_PROMPT.md \
  --exclude docs/PUBLIC_GITHUB_READINESS_AUDIT_REPORT.md \
  --exclude 'Gemini_Generated_Image_*'
cd ./mcp-multiplex-public

# Verify the public tree before creating history.
! rg -n '/home/(aj|core)' . --glob '!tests/**' --glob '!PUBLICATION.md'
! rg -n \
  '(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[0-9A-Z]{12,})' \
  .
uv sync --locked --dev
make check
make build

# Create the public history.
git init --initial-branch=main
git add --all
git commit -m "feat: initial public release of MCP Multiplex

Clean history for GitHub publication.

- Added Apache-2.0 licensing and open source project scaffolding
- Added uv-based CI, contribution, and security workflows
- Documented the governed MCP control plane and runtime proxy
- Removed private development checkpoints"

git remote add origin git@github.com:AmeerJ97/mcp-multiplex.git
git push --set-upstream origin main
git tag -a v0.1.0 -m "MCP Multiplex v0.1.0"
git push origin v0.1.0
```

This strategy preserves the complete private repository and avoids force
pushing altogether.

## Alternative: Filter A Disposable Clone

Use this only when preserving selected private commits has value:

```bash
git clone --mirror ./mcp-multiplex ./mcp-multiplex-backup.git
git clone --no-hardlinks ./mcp-multiplex ./mcp-multiplex-filtered
cd ./mcp-multiplex-filtered

git filter-repo \
  --path .codex-checkpoints/ \
  --path docs/checkpoints/ \
  --path docs/FRESH_SESSION_PROMPT.md \
  --path docs/GITHUB_POLISH_MASTER_PROMPT.md \
  --path docs/PUBLIC_GITHUB_READINESS_AUDIT_REPORT.md \
  --path 'Gemini_Generated_Image_*' \
  --invert-paths \
  --force
```

Review every remaining revision before publishing:

```bash
git log --all --stat
git grep -n -i -E 'api[_ -]?key|password|raw-secret|/home/(aj|core)' \
  $(git rev-list --all) -- ':!tests/**' ':!PUBLICATION.md'
```

If a single public root commit is still preferred, export the filtered working
tree using the recommended clean-export strategy above. Do not force-push over
an existing shared public branch without coordinating with every contributor.

## Ongoing History

Protect `main`, require CI, require pull requests, and prefer squash merges.
Squash merging keeps the public history linear without hiding the review
history attached to each pull request.

## Recommended GitHub Settings

Repository description:

```text
Daemon-first local governance and convergence for Model Context Protocol servers across coding agents.
```

Website:

```text
https://modelcontextprotocol.io
```

Topics:

```text
mcp
model-context-protocol
ai
ai-agents
coding-agents
cli
python
governance
daemon
proxy
```

Enable:

- Issues and private vulnerability reporting;
- dependency graph, Dependabot alerts, and secret scanning;
- branch protection for `main`;
- required pull requests and required `CI / Python 3.12` and
  `CI / Python 3.13` checks;
- deletion of head branches after merge;
- squash merge as the default merge strategy.

## PyPI Trusted Publishing

No automatic release workflow is enabled yet because the PyPI project and
GitHub environment must be created by the repository owner first.

After claiming `mcp-multiplex` on PyPI:

1. Create a protected GitHub environment named `pypi`.
2. Add a PyPI trusted publisher for repository
   `AmeerJ97/mcp-multiplex`, workflow `release.yml`, environment `pypi`.
3. Add a tag-triggered workflow that builds with `uv build`, verifies with
   `twine check`, and publishes with `uv publish --trusted-publishing always`.
4. Require the CI workflow to pass before creating the release tag.

The official uv guide documents the current trusted-publishing setup:
<https://docs.astral.sh/uv/guides/integration/github/>.
