# MXP CLI Guide

> **Release status:** The command surface documented here is implemented.
> Client configuration formats can change independently, so
> `mxp agents self-check` and `mxp doctor release-gate` are authoritative for
> machine-level readiness. Streamable HTTP control-plane details are documented
> in [Control-Plane Protocol Status](CONTROL_PLANE_STATUS.md).

`mxp` is the short operator and agent-facing CLI for MCP Multiplex.
`mcp-multiplex` remains a compatibility alias for scripts that already use the
long-form name.

## Command Shape

```bash
mxp health
mxp daemon install-user-service
mxp daemon status
mxp status --json
mxp config inspect
mxp config discover
mxp audit run
mxp plan list
mxp plan show <plan-id>
mxp approval list
mxp approval approve <approval-id>
mxp catalog review <catalog-id> --review-state approved --lifecycle-state enabled
mxp apply <plan-id>
mxp rollback <backup-id>
mxp runtime ps
mxp runtime why-slow --server <server>
mxp tui
mxp tui --repl
mxp agents auth-capabilities
mxp agents self-check
mxp agents self-check --agent codex
mxp certify codex
mxp certify import-evidence --checkpoint-dir docs/certifications
mxp cutover dry-run --from mcp-hub --legacy-root "$HOME"
mxp cutover import-catalog --from mcp-hub --catalog-path ./mcp-hub-catalog.json
mxp cutover apply --from mcp-hub --confirm-retire-mcp-hub
mxp cutover status --check-gate --check-footprint
mxp cutover legacy-footprint
mxp cutover legacy-cleanup-plan
mxp doctor release-gate
mxp doctor release-gate --global-cutover
mxp doctor retirement-gate
```

All mutation commands are expected to be backed by exact-byte backups,
post-write verification, audit events, and rollback metadata.

## Daemon User Service

MCP Multiplex is daemon-first. Install the local daemon as a systemd user service
only after reviewing the generated unit:

```bash
mxp daemon install-user-service --home "$HOME"
mxp daemon install-user-service --home "$HOME" --apply
mxp daemon status --home "$HOME"
```

The first command is a dry run. It renders the `mcp-multiplex.service` unit that
binds the daemon to `127.0.0.1:30000` and points it at Governor's local SQLite
state. The apply command writes only the user unit file, creates an exact-byte
backup when replacing an existing unit, verifies the written unit, and records a
redacted audit event in Governor state.

Service start and enable remain explicit operator actions:

```bash
systemctl --user daemon-reload
systemctl --user enable --now mcp-multiplex.service
```

The command does not install `mcp_hub` into any agent config, import legacy
catalog entries, stop `mcp-hub`, or start/kill unmanaged processes.

`mxp daemon status` is read-only. It reports the expected user unit path,
whether the unit file exists, the unit file hash, and systemd's `LoadState`,
`ActiveState`, `SubState`, and `UnitFileState` when `systemctl --user show` is
available.

## Authenticated Control-Plane Install

The `mcp_hub` control-plane MCP is installed as a named MCP entry. It is not an
omnibus data-plane MCP and does not re-export downstream server tools.

Codex, Claude Code, Cline, Gemini, and OpenCode are automatic install targets
because they can avoid raw token storage in durable config. Codex supports an
environment variable reference for the bearer token:

```bash
mxp agents install-control-plane --agent codex --home "$HOME"
mxp agents install-control-plane --agent codex --home "$HOME" --apply
```

The first command is a dry run. The second command:

- creates or updates the Codex user config,
- writes the `mcp_hub` URL,
- writes `bearer_token_env_var = "MCP_MULTIPLEX_CONTROL_TOKEN"`,
- issues an agent-scoped `control:read` token,
- stores only the token ref and token hash in Governor state,
- creates an exact-byte backup before mutation,
- verifies the config parses back as an authenticated `mcp_hub` entry.

The raw token is redacted from JSON output by default:

```bash
mxp agents install-control-plane --agent codex --apply --home "$HOME"
```

Use one-time token output only in a controlled shell:

```bash
mxp agents install-control-plane --agent codex --apply --emit-token --home "$HOME"
```

Then export the token before launching Codex:

```bash
export MCP_MULTIPLEX_CONTROL_TOKEN="<one-time-token-output>"
codex
```

Do not paste the token into `~/.codex/config.toml`, project files, shell
history, logs, checkpoints, or docs.

Claude Code uses `headersHelper` instead of a static `Authorization` header:

```bash
mxp agents install-control-plane --agent claude_code --home "$HOME"
mxp agents install-control-plane --agent claude_code --home "$HOME" --apply
```

The apply command writes `mcp_hub` to `~/.claude.json` with:

- `"type": "http"`,
- `"url": "http://127.0.0.1:30000/servers/mcp_hub/mcp"`,
- `headersHelper` pointing to Governor's local helper script.

The helper script reads `MCP_MULTIPLEX_CONTROL_TOKEN` at connection time and
prints the `Authorization` header JSON expected by Claude Code. The raw token is
not written to `~/.claude.json` or the helper script.

OpenCode supports environment-variable expansion in remote MCP headers:

```bash
mxp agents install-control-plane --agent opencode --home "$HOME"
mxp agents install-control-plane --agent opencode --home "$HOME" --apply
```

The apply command writes `mcp_hub` to `~/.config/opencode/opencode.json` with
`oauth: false` and:

```json
{"Authorization": "Bearer {env:MCP_MULTIPLEX_CONTROL_TOKEN}"}
```

The raw token is not written to the OpenCode config.

Gemini uses a remote MCP `httpUrl` plus a headers object with an env template:

```bash
mxp agents install-control-plane --agent gemini --home "$HOME"
mxp agents install-control-plane --agent gemini --home "$HOME" --apply
```

The apply command writes `mcp_hub` to `~/.gemini/settings.json` with:

```json
{
  "httpUrl": "http://127.0.0.1:30000/servers/mcp_hub/mcp",
  "headers": {
    "Authorization": "Bearer $MCP_MULTIPLEX_CONTROL_TOKEN"
  },
  "trust": false
}
```

The raw token is not written to the Gemini settings file.

Cline uses a Governor-owned helper script that runs `mcp-remote` as a stdio MCP
process and injects the runtime bearer header when connecting to the local Hub
URL:

```bash
mxp agents install-control-plane --agent cline --home "$HOME"
mxp agents install-control-plane --agent cline --home "$HOME" --apply
```

The apply command writes `mcp_hub` to Cline's MCP settings with a `command`
pointing at Governor's helper script. The helper executes:

```text
npx -y mcp-remote http://127.0.0.1:30000/servers/mcp_hub/mcp --header "Authorization: Bearer $MCP_MULTIPLEX_CONTROL_TOKEN"
```

The raw token is not written to Cline settings or the helper script. Cline must
be able to run `npx` and resolve `mcp-remote` when it starts the MCP entry.
When overriding the helper path, the filename must remain
`cline-mcp-multiplex-remote.sh` so Governor can recognize the entry as its own
authenticated control-plane projection.

## Auth Capability Matrix

Use the auth capability matrix to see the current reason for each block:

```bash
mxp agents auth-capabilities
```

Current classifications:

- `codex`: implemented with `bearer_token_env_var`; no raw token is stored.
- `claude_code`: implemented with `headersHelper`; no raw token is stored.
- `gemini`: implemented with `Authorization: Bearer $MCP_MULTIPLEX_CONTROL_TOKEN`;
  no raw token is stored.
- `cline`: implemented with a Governor helper that runs `mcp-remote`; no raw
  token is stored.
- `opencode`: implemented with `Bearer {env:MCP_MULTIPLEX_CONTROL_TOKEN}` and
  `oauth: false`; no raw token is stored.

The release gate fails any enabled `mcp_hub` entry that lacks `Authorization`
header metadata.

## TUI

`mxp tui` renders the current operator surface:

```bash
mxp tui
mxp tui --repl
mxp tui --approve <approval-id>
```

`mxp tui --repl` starts a scriptable operator REPL with a branded header and
slash-command support. It is intentionally dependency-free and can be driven by
humans or agents:

```text
MCP Multiplex
local MCP control plane // governed catalog // runtime proxy

mxp> dashboard
mxp> self-check
mxp> commands
mxp> problems
mxp> cutover
mxp> approvals
mxp> approve <approval-id>
mxp> candidates
mxp> runtime
mxp> credentials
mxp> rollback
mxp> changes
mxp> quit
```

`self-check` is the agent-facing readiness view. It lists registered agents,
their watched config paths, and whether an authenticated `mcp_hub` control-plane
entry is currently observed. `commands` prints the slash-command registry with
aliases so humans and agents can discover the REPL surface without reading this
file.

`cutover` is the REPL retirement view. It shows whether an audited
`cutover.applied` event exists, whether the legacy MCP Hub footprint is clean,
and which cleanup steps remain. It is read-only and does not apply cleanup.

For non-interactive agents, use the CLI form:

```bash
mxp agents self-check --home "$HOME"
mxp agents self-check --agent codex --home "$HOME"
```

The command emits `MCPMultiplexAgentSelfCheck` JSON and exits `0` only when every
selected registered agent is `ready`. It exits `1` when an agent has no observed,
authenticated `mcp_hub` entry and needs install or audit work.

The REPL can approve pending plans through the same TUI operator channel as
`mxp tui --approve`. It does not auto-apply plans, rewrite configs, start
backends, or expose raw secret references.

## Cutover Status

The first cutover command is read-only:

```bash
mxp cutover dry-run --from mcp-hub --legacy-root "$HOME"
```

It discovers supported first-wave agent configs under the supplied legacy root,
parses them, classifies direct bypasses and Hub-routed entries, verifies the
source files were not mutated during analysis, and reports next actions. It does
not import catalog entries, install auth, rewrite configs, stop processes, or
disable legacy MCP Hub.

Legacy MCP Hub catalog import is also dry-run by default:

```bash
mxp cutover import-catalog --from mcp-hub --catalog-path ./mcp-hub-catalog.json
mxp cutover import-catalog --from mcp-hub --catalog-path ./mcp-hub-catalog.json --apply
```

The importer accepts a legacy JSON export containing a `servers` list, a
`catalog` list, or a `servers` object keyed by server name. It normalizes each
server into a Governor catalog entry, preserves the named per-server URL
contract, records provenance back to the legacy export, and stages entries as
`pending` unless the export already carries a supported review state. Legacy env
objects are converted to env names only; raw credential values are rejected.

Apply writes only Governor catalog rows and a redacted audit event. It does not
rewrite agent configs, approve entries for routing, stop `mcp-hub`, or kill
unmanaged processes.

Imported entries are usually staged as `pending`. After inspecting the entry's
backend, credentials, shareability, and provenance, approve it explicitly:

```bash
mxp catalog review srv_context7 \
  --review-state approved \
  --lifecycle-state enabled \
  --comment "reviewed legacy import"
```

The review command records a `catalog.reviewed` audit event with before/after
hashes and reports whether the entry is routable. It does not mutate agent
configs or start backend processes.

Global MCP Hub retirement must wait until:

- authenticated `mcp_hub` install is certified for every target client,
- certification checkpoint hashes are imported into the Governor audit chain,
- the daemon user service is installed and reviewed,
- legacy MCP Hub catalog entries are imported, reviewed, or staged as candidates,
- `mxp cutover dry-run --from mcp-hub` exists and reports no unsafe mutation,
- `mxp doctor release-gate --global-cutover` passes,
- rollback artifacts exist for every planned config mutation.

Before running the global gate, bind the current real-client certification
transcripts to the local audit chain:

```bash
mxp certify import-evidence --checkpoint-dir docs/certifications --home "$HOME"
```

This command reads the expected certification checkpoint files, verifies each
contains `Result: PASS`, records each transcript hash as a
`certification.evidence_imported` audit event, and stores no raw secrets. The
global gate fails if the checkpoint files change after import.

After those gates pass, record the audited cutover:

```bash
mxp cutover apply --from mcp-hub --confirm-retire-mcp-hub --home "$HOME"
mxp cutover status --home "$HOME" --check-gate --check-footprint
mxp cutover legacy-footprint --home "$HOME"
mxp cutover legacy-cleanup-plan --home "$HOME"
mxp doctor retirement-gate --home "$HOME"
```

`cutover apply` always re-runs the global release gate before recording the
retirement event. It fails closed when the gate does not pass and requires the
explicit `--confirm-retire-mcp-hub` flag. The command records a
`cutover.applied` audit event with `legacy_mcp_hub_deprecated=true`.

`cutover status` is the read-only proof command for humans and agents after
apply. It emits `MCPMultiplexCutoverStatus` JSON with the latest matching
`cutover.applied` audit event and exits `0` only when MCP Hub retirement is
recorded. Add `--check-gate` to re-run the current global release gate and make
the command fail if the machine has drifted away from the retirement-ready
state. Add `--check-footprint` to include the same bounded legacy footprint
inspection as `cutover legacy-footprint`; the combined status then exits `1`
when old MCP Hub artifacts or active processes still require explicit operator
cleanup.

`cutover legacy-footprint` is the read-only retirement cleanup report. It
checks the explicit legacy root, defaulting to `~/mcp-hub`,
for bounded evidence such as `.git`, `hub.json*`, `launch-hub.py`,
`launch-hub.sh`, and candidate user-service unit files. Unless
`--no-processes` is passed, it also inspects `ps` output for MCP Hub-like
processes. A matching process makes the command exit `1` and reports an
operator action. Repository, launch-script, catalog-export, and service-unit
evidence also make the command exit nonzero because the final retirement
footprint is not clean while those artifacts remain. In all cases,
`unmanaged_process_action` remains `none`: Governor does not stop, kill, delete,
or archive legacy MCP Hub as a side effect.

`cutover legacy-cleanup-plan` converts the same footprint evidence into explicit
operator-reviewed cleanup steps such as disabling a legacy user service,
uninstalling a globally resolvable legacy executable, stopping an identified
legacy process through its owner, or archiving the old repository. It is
planning-only: `apply_supported=false`, `mutation_action=none`, and destructive
steps are marked `approval_required=true`.

`doctor retirement-gate` is the final read-only completion gate for declaring
legacy MCP Hub retired on the machine. It combines the global release gate, the
audited `cutover.applied` event, and the legacy cleanup plan. It exits `0` only
when Governor is globally healthy, cutover has been recorded, and the legacy
footprint has no remaining cleanup steps.

It does not silently kill or stop unmanaged legacy MCP Hub processes. If a
legacy service manager exists, stopping it remains a separate operator action
after the audited Governor cutover is recorded.

`mxp doctor release-gate` remains the local integrity gate. Add
`--global-cutover` before retiring MCP Hub. The global gate also requires
hash-bound certification evidence imported with `mxp certify import-evidence`:

```bash
mxp doctor release-gate --global-cutover --home "$HOME"
```

Global mode adds retirement-specific checks:

- `control_plane_auth_capabilities`: every first-wave agent must have an
  implemented automatic authenticated `mcp_hub` installer that does not require
  raw token storage.
- `certification_evidence_hashes`: imported certification evidence must match
  the current checkpoint transcript hashes.
- `daemon_user_service`: `mcp-multiplex.service` must be installed, loaded,
  active, and enabled as a user service.
- `legacy_catalog_import`: a `catalog.legacy_import` audit event must exist,
  and all catalog entries with `legacy_mcp_hub` provenance must be approved and
  enabled.

For isolated validation environments, `--daemon-unit-dir` and
`--daemon-systemctl-bin` can point the gate at fixture paths. Do not use fixture
paths for real machine cutover.
