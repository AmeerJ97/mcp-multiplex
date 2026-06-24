---
name: Bug report
about: Report reproducible incorrect behavior
title: "bug: "
labels: bug
assignees: ""
---

## Summary

Describe the observed behavior and the expected behavior.

## Environment

- MCP Multiplex version or commit:
- Operating system:
- Python version:
- `uv --version`:
- Affected agent/client:

## Reproduction

Provide the smallest redacted fixture or command sequence that reproduces the
problem. Do not include tokens, private configs, or credential values.

## Verification

Include relevant exit codes, sanitized JSON output, and tests already attempted.

## Safety Impact

State whether the issue involves config mutation, rollback, process lifecycle,
credential handling, direct bypass detection, or runtime session isolation.
