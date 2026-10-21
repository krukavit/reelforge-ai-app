# ReelForge Agent Next — Installation Package
## Backup retention

Agent Next has bounded automatic backup retention. Successful backups are tracked in StateStore and, after each new successful backup, only the newest `BACKUP_MAX_PER_TARGET` backups for that target are retained (default: 5). Cleanup is deliberately narrow: it can delete only regular files already recorded by Agent Next as successful backups; it never scans arbitrary directories, follows symlinks, or removes directories. The retention limit can be overridden with `AGENT_NEXT_BACKUP_MAX_PER_TARGET`.

This prevents the phone from accumulating unlimited backup archives while preserving a recent rollback window. Old/foreign backups that are not recorded as Agent-Next backups are not touched automatically.


Build: 2026-09-18-final-source
Package status: FINAL SOURCE CANDIDATE — STAGE 1/2/3.1/4 LOCAL GATES GREEN; LIVE ACCEPTANCE REMAINS A SEPARATE GATE

## Purpose

Clean, secret-free installation snapshot for Termux. Runtime state and credentials are owned by Agent Next after installation.

## Safety boundaries

- Do NOT recreate or use the legacy Agent as a runtime dependency. The legacy Agent remains preserved separately until Agent Next completes independent final verification and explicit retirement approval.
- Do NOT modify the production ReelForge AI application.
- Do NOT create a second MCP Core, Bridge, or transport.
- Reuse the existing Native MCP Core (`127.0.0.1:8081`), protected Bridge (`127.0.0.1:18081`) and existing transport.
- `run` and `session_run` remain blocked.
- No ADB, Accessibility, Android daemon, second transport, cloud orchestration, or full voice client.
- Durable runtime state belongs under this package's `state/`.
- Secrets are NOT included in this archive.

## Install

Recommended target: `~/reelforge-agent-next`. Preserve any existing Next directory before replacement. The legacy `~/reelforge-agent` is not a runtime dependency and is not included in this package.

### One-time credential migration

Agent Next must not use the old Agent directory as a runtime credential dependency. The archive contains no credentials. The Bridge credential must already exist in an Agent-Next-owned secure path, or be supplied via `REELFORGE_MCP_TOKEN_FILE`. Do not print or copy secrets into the project tree.

### MCP configuration

The source package includes `config/mcp.example.txt`, a generic configuration
template. A fresh installation copies it to `config/mcp.txt`. When replacing
an existing installation, the installer preserves that installation's
`config/mcp.txt` instead of overwriting it with the template.

`config/mcp.txt` is installation-specific and must not be committed or
distributed. The token itself belongs in a separate protected file referenced
by `MCP_TOKEN_FILE` or `REELFORGE_MCP_TOKEN_FILE`; never put the token in the
configuration file or source tree.

## First checks

From the installed project root:

    python -m compileall -q app tests
    python tests/run_all.py

The suite includes concurrent confirmation-consumption, task-lifecycle, atomic-completion, and unknown-outcome regression checks.

Live MCP acceptance remains a separate gate: initialize -> tools/list (with pagination) -> health -> authenticated read-only call -> real confirmation dispatch -> recovery/E2E.

## Target architecture

ChatGPT / LLM -> Agent Next -> Policy -> Runner/Executor -> MCP Worker/Client -> authenticated Bridge :18081 -> existing Native MCP Core :8081 -> Termux capabilities

The LLM is proposer/planner only; it never grants authority.

## Current readiness

NOT CUTOVER READY until live capability discovery, authenticated calls, real confirmation dispatch, restart/recovery, and full E2E pass.


## Revision r7
Confirmation challenges are bound to the exact task step; while a task is BLOCKED_ON_USER_ACTION no new step may be appended. This prevents a pending confirmation from being redirected to another step.

## r10 normalization
Configuration text is aligned with the runtime policy and runner behavior: backup is VALIDATED, execution is implemented, and the live MCP gate remains mandatory before cutover.


## r11 (2026-09-13)

- MCP Worker now records each dispatched MCP call in Agent-Next StateStore with task linkage and SUCCESS/FAILED/UNKNOWN_OUTCOME status.
- MCP JSON-RPC responses must declare `jsonrpc: 2.0` and match the request id.
- No automatic replay of `tools/call`; uncertain mutation outcomes remain non-retryable.
- Profile enforcement was intentionally not added: Policy remains the single capability authorization boundary until profile semantics are specified and tested end-to-end.


## r13 (2026-09-13)

- Confirmation resume is restart-safe: a persisted BLOCKED task is re-authorized only with the consumed challenge, then resumed.
- Runner explicitly rejects unconfirmed authorization while BLOCKED_ON_USER_ACTION.
- Added regression coverage for confirmation persistence across new StateStore/Executor instances.
- StateStore schema constant is aligned with the on-disk schema version (4).

## r12 (2026-09-13)

- MCP audit records now retain operation, request/session/protocol metadata and a SHA-256 hash of canonical arguments; raw arguments are not stored.
- StateStore schema version is 4 with migration coverage from the previous MCP audit schema.
- UNKNOWN_OUTCOME is classified by exception type rather than matching error text.
- Deterministic test suite now includes MCP audit, migration, and unknown-outcome coverage.

## Archive repair r15-clean (2026-09-13)
- Synchronized test doubles with the production MCPWorker contract: `call_tool(..., task_id=...)` is accepted by executor/retention fakes.
- Added an explicit assertion that Executor forwards the originating task_id to the worker.
- This archive is a repaired source snapshot; live Agent Next and production remain untouched during repair.
- Archive-integrity block: restored the required 57-tool bootstrap registry, removed generated bytecode from the distribution, removed runtime `sys.path` mutation, and aligned package config/docs with the runtime policy and current architecture.

## Archive repair verification r15-clean
- `python -m compileall -q app tests`: PASS.
- `tests/run_all.py`: PASS, `ALL_LOCAL_TESTS_OK` (all discovered function tests plus standalone checks/probes).
- Standalone probes must be run from the project root as modules, e.g. `python -m tests.probe_workers`; direct `python tests/probe_workers.py` is not a supported invocation because Python does not add the project root to `sys.path`.
- Secret-pattern scan: 0 hits.
- No live Agent Next, MCP Core/Bridge, or production process was modified by this archive repair.


## Final source audit r15-final

- Fresh archive extraction and source audit completed before packaging.
- Bootstrap capability registry: 57 unique live capability definitions.
- Policy coverage: all 57 registry capabilities are classified; `run` is intentionally absent from the registry and `run`/`session_run` are globally blocked by Policy.
- MCP connection contract: Agent Next targets only the existing authenticated Bridge at `127.0.0.1:18081/mcp`, which fronts the existing Native MCP Core at `127.0.0.1:8081`; no duplicate MCP/Bridge/transport is packaged.
- No legacy-Agent runtime import/path dependency is present in Python source.
- No credentials, runtime database, bytecode, or `__pycache__` are distributed.
- Full local test suite and compile checks passed.
- Live Bridge/Core connectivity is deliberately a post-install acceptance gate; this source archive does not claim to replace that live verification.

## r16 installation procedure
The archive includes `INSTALL.sh` for a controlled one-shot installation. It never copies credentials and, when replacing an existing Agent Next, preserves the existing shared MCP `ops/` stack while creating a timestamped backup first.

From the extracted archive root:

    ./INSTALL.sh --replace

Then run the documented local suite and the live acceptance gate. Do not overwrite the shared MCP stack merely by extracting this archive over the live tree.


## Stage 3.1 — Master Workflow CLI

Stage 3.1 adds the deterministic Master Workflow CLI on top of the Stage 2
master/child orchestration layer.

Supported commands:

    python3 agent.py master-create --task-id <master_id>
    python3 agent.py master-add-stage1 <master_id> <child_id> [--profile EDIT] [--depends-on <child_id>[,<child_id>...]]
    python3 agent.py master-start-child <master_id> <child_id>
    python3 agent.py master-status <master_id>
    python3 agent.py master-reconcile <master_id>

The CLI uses the same SQLite/WAL StateStore and existing Runner/Policy/Executor
path. It does not create a second database, MCP stack, Bridge, transport, or
runtime agent.

Stage 3.1 verification completed locally with the isolated CLI contract,
21/21 local test files passing, and compileall passing. Live MCP acceptance
remains a separate gate.

## Agent Next entrypoints

- `python3 agent.py self-test` — initializes/validates durable StateStore and capability registry.
- `python3 agent.py health` — executes the real deterministic health path through TaskExecutor.
- `python3 agent.py registry` — validates and prints the local capability inventory.
- `python3 agent.py status` — reports persistent task/worker state and stuck tasks.
- `python3 agent.py recover` — explicitly recovers RUNNING tasks after a restart/failure reason.
- `python3 agent.py tool <capability> '<json>'` — executes only through Policy/Runner/Executor; arbitrary shell is not exposed.
- `python3 agent.py confirm <task_id> <confirmation_token>` — resumes only a persisted, exact confirmation challenge.
- `./verify.sh` — compile + complete local suite + Agent self-test.

The installer copies the Agent Next source/config/test files into the target. Existing shared MCP/Core/Bridge/TNNL stack and runtime state are preserved on `--replace`.


### Fresh-install behavior
A fresh target does not receive an `ops/` directory. The shared MCP/Core/Bridge/TNNL stack is external to the Agent Next package and is never created, replaced, or copied by this installer. An existing target preserves its already-proven shared stack and runtime state.

## Post-Stage 4 verification gates — 2026-09-18

The following source-level gates are closed and included in this final-source candidate:
- Capability Inventory: live `tools/list` reconciliation, deterministic fingerprinting, atomic persistence; `CAPABILITY_INVENTORY_TESTS=10/10`.
- Authorization boundary: metadata, tool output, and LLM claims cannot grant permission; mutation confirmation remains explicit; unknown capabilities and always-blocked tools remain denied.
- Recovery Reason Matrix: exact canonical reasons include `PROCESS_RESTART`, `BRIDGE_RESTART`, `CORE_RESTART`, `NETWORK_FAILURE`, `USER_ACTION`, `TOOL_FAILURE`, `POLICY_BLOCK`, `BACKUP_VERIFY_FAILED`, and `UNKNOWN`; `tests/test_recovery_reason_matrix.py`: 4/4 passed.
- Audit Event ID: existing `events.id INTEGER PRIMARY KEY AUTOINCREMENT` is returned by `StateStore.add_event()`; no schema migration was required; `tests/test_event_id.py`: 1/1 passed.
- Full local regression remained `ALL_LOCAL_TESTS_OK`.
- Stage 1 regression: 13/13.
- Stage 4 regression: 6/6.
- Production mutation: NONE.

## Stage 4 checkpoint — 2026-09-17

Stage 4 adds deterministic master-plan orchestration for eight Stage 1 child
workflows. Planning and registration are separated from execution: building
the plan performs no worker/MCP execution, and child workflow registration is
atomic.

Verified Stage 4 contract:
- master workflow created successfully;
- 8 deterministic Stage 1 child workflows registered;
- each child contains 8 Stage 1 steps;
- total registered child steps: 64;
- explicit dependency metadata is present;
- invalid profiles are rejected;
- partial child registration rolls back atomically;
- planning performs zero MCP calls.

Stage 4 verification:
- `tests/test_stage4_orchestrator.py`: 6/6 passed;
- full project runner: `ALL_LOCAL_TESTS_OK`;
- full project runner exit code: 0;
- existing Stage 1/Stage 2 contracts remain green;
- no production mutation;
- no live MCP dispatch was required for this source-stage gate.

The project verification entrypoint is `python3 -m tests.run_all`.
Pytest and unittest discovery are not required by this project test harness.
