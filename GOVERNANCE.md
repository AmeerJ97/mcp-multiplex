# Governance

MCP Multiplex is maintained as an open core project.

## License

The open source core is licensed under the Apache License 2.0. Contributions to
the open source repository are expected to be submitted under Apache 2.0 unless
the maintainers explicitly agree otherwise in writing.

## Project Boundary

The open source core includes the local daemon, CLI, policy and approval model,
runtime proxy, catalog schema, public adapters, tests, and user-facing
documentation needed to run MCP Multiplex on a single machine.

Future commercial extensions may add organization-level capabilities such as
fleet management, centralized policy distribution, compliance reporting,
enterprise identity integration, managed hosting, and support services.

## Contributions

Contributions should be focused, reviewable, and aligned with the safety model:

- no raw secrets, tokens, private configs, or credential-bearing logs;
- no mutation paths without dry-run planning, approval, backup, verification,
  and rollback behavior;
- tests proportional to the behavior changed;
- documentation updates for user-visible behavior.

By submitting a pull request, you confirm that you have the right to submit the
contribution under Apache 2.0.

The maintainers may decline features that are too broad, weaken the safety
model, or belong in future enterprise extensions.
