## Summary

Describe the behavior changed and why.

## Safety Review

- [ ] No raw secret values were added to code, tests, logs, or documentation.
- [ ] Config mutations remain backed up, auditable, verified, and reversible.
- [ ] Destructive or ambiguous actions remain approval-gated.
- [ ] Runtime sharing and agent rewrite behavior remain policy/certification gated.
- [ ] I have the right to submit this contribution under the Apache License 2.0.

## Verification

- [ ] `uv run ruff format --check .`
- [ ] `uv run ruff check .`
- [ ] `uv run mypy src tests`
- [ ] `uv run pytest -q --tb=short`
- [ ] `uv build`

List any additional real-client or daemon verification:

## Documentation

- [ ] CLI, policy, protocol, and migration documentation is updated where needed.
