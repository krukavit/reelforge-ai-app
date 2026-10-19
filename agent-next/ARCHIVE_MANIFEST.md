# ReelForge Agent Next — Archive Manifest / РЕЗЮМЕ-ДОСЬЕ
## Backup retention

Agent Next has bounded automatic backup retention. Successful backups are tracked in StateStore and, after each new successful backup, only the newest `BACKUP_MAX_PER_TARGET` backups for that target are retained (default: 5). Cleanup is deliberately narrow: it can delete only regular files already recorded by Agent Next as successful backups; it never scans arbitrary directories, follows symlinks, or removes directories. The retention limit can be overridden with `AGENT_NEXT_BACKUP_MAX_PER_TARGET`.

This prevents the phone from accumulating unlimited backup archives while preserving a recent rollback window. Old/foreign backups that are not recorded as Agent-Next backups are not touched automatically.


Build: 2026-09-24-final-source
Status: FINAL SOURCE — LOCAL + LIVE ACCEPTANCE GATES GREEN; CLEAN EXTRACTION/INSTALL VERIFICATION GREEN

## Что внутри
- `agent.py`: deterministic Agent Next CLI over TaskExecutor/Runner/Policy/MCPWorker; no arbitrary shell interface.
- `verify.sh`: single local verification entrypoint for compile/tests/self-test/registry.
- `app/`: runtime orchestration, policy, state, confirmation, MCP client/worker, recovery.
- `config/`: operational contracts and MCP configuration.
- `state/capability_registry.json`: clean bootstrap capability snapshot used for offline validation; live `tools/list` remains the runtime source of truth and must refresh/reconcile this inventory.
- `tests/`: bottom-up checks plus `tests/run_all.py`. Standalone probes are invoked as Python modules from the project root (for example, `python -m tests.probe_workers`).
- `INSTALL.md`: safe installation and one-time credential migration instructions.
- `ARCHIVE_MANIFEST.md`: this dossier.

## Что намеренно НЕ входит
- secrets/tokens/keys;
- runtime SQLite databases;
- diagnostic captures;
- development backups;
- old Agent source/runtime;
- production application;
- second MCP/Bridge/transport.

## Архитектура
ChatGPT/LLM (планирование) -> Agent Next (оркестрация) -> Policy (полномочия) -> Runner/Executor -> MCP Worker/Client -> существующий Bridge :18081 -> существующий Core :8081 -> Termux/OS. TNNL остаётся только транспортом.

## 2026-09-23 — Live acceptance checkpoints

- Persisted immutable `target_root` contract is implemented across StateStore, Runner, Workflow, MasterWorkflow, Orchestrator and Executor.
- Target-root boundary tests are GREEN, including parent escape, external absolute path and symlink escape.
- Live target-root Stage 1 workflow E2E is GREEN; backup/verify/diagnose/read/plan completed and edit remained correctly blocked on confirmation.
- Real confirmed MCP `write` dispatch is GREEN on an isolated E2E target.
- Real MCP write audit is GREEN with live session, protocol `2025-06-18`, request ID and SUCCESS status persisted in StateStore.
- Real MCP `write -> read` roundtrip is GREEN; the written artifact was subsequently read back through the same Agent Next -> MCP Worker -> MCP Client -> Bridge/Core path.
- Full local regression is GREEN: `ALL_LOCAL_TESTS_OK`, including target-root, confirmation, recovery, WAL, MCP safety, Stage 1 and Stage 4 suites.
- Live capability reconciliation remains 57/57 with `run` and `session_run` globally blocked.
- No production ReelForge mutation occurred during these gates.

Key evidence markers:
`LIVE_TARGET_ROOT_E2E_WORKFLOW=GREEN`
`REAL_MCP_WRITE_DISPATCH=GREEN`
`REAL_MCP_AUDIT=GREEN`
`REAL_MCP_DISPATCH_PROVEN=YES`
`REAL_MCP_WRITE_READ_ROUNDTRIP=GREEN`
`REAL_MCP_READ_VERIFIED=YES`
`ALL_LOCAL_TESTS_OK`
`PRODUCTION_MUTATION=NONE`

## Security
`run` и `session_run` ALWAYS_BLOCK. CONFIRM требует одноразовый токен, привязанный к task/capability/operation. VALIDATED не заменяет CONFIRM. LLM не может выдать себе confirmed.

## Persistence/recovery
SQLite WAL; durable state under `state/`; recovery distinguishes restart failures. Mutating operations must not be blindly retried after an unknown outcome.

## Исправления этой ревизии
0. Runner now rejects cross-task step authorization and adding/authorizing steps on terminal tasks.
1. Capability registry rejects duplicate or nameless tools and reports total vs unique counts.
2. MCP JSON-RPC errors are surfaced as failures rather than accepted as successful responses.
3. Confirmation consumption is serialized with SQLite `BEGIN IMMEDIATE` and a conditional one-time update; a concurrent-consumer regression test is included.
4. Installation package now contains the required bootstrap capability registry.
5. Runtime paths resolve from the Agent Next project root rather than the caller's current directory.
6. Test scripts no longer depend on `~/reelforge-agent-next` or require manual `PYTHONPATH`.
7. Validated authorization is forwarded all the way from Executor to MCP Worker.
8. Confirmation tokens are bound to task/capability/operation and canonical argument hash.
9. MCP tools/list supports pagination; mutation calls are not blindly replayed after connection loss.
10. Tests no longer use the old Agent credential path.
11. Bridge credential is Agent-Next-owned after a one-time local migration; archive remains secret-free.
12. Registry validation no longer assumes a hard-coded 57-tool count.

## Verification status
Local static/unit/probe tests pass in the build workspace; the archive test runner executes all discovered function tests plus standalone checks/probes. Live Bridge/Core tests remain mandatory before FINAL/CUTOVER. Live MCP confirmation-path acceptance remains an external gate and is not hidden.

## What not to run/delete
Do not run arbitrary shell through Agent Next. Do not recreate the legacy Agent or touch production. Do not create duplicate infrastructure.

## Final archive rule
A future FINAL/CUTOVER archive may be labeled ready only after: live capability fingerprint, authenticated read-only call, real confirmation dispatch, failure/restart recovery, full E2E, dependency/secret scan, clean extraction/reinstall test, and SHA256 recorded.


## Revision r7 (2026-09-13)
- Confirmation records are bound to exact task step IDs.
- Blocked tasks cannot receive additional steps while a confirmation is pending.
- SQLite schema version is 3 with confirmation step_id migration and unique task step numbers.
- Added regression coverage for step binding and blocked-step injection.
- Direct execution of package tests is path-independent where applicable.


## Revision r7 (2026-09-13)
- MCP JSON-RPC responses are bound to the originating request ID; mismatched responses are rejected.
- Worker heartbeats are accepted only while the worker is RUNNING; stopped workers cannot be silently resurrected.
- Added regression coverage for both boundaries.

## Revision r7 (2026-09-13)
- Task completion is guarded: a task cannot become COMPLETED without at least one completed step.
- Step completion + task completion is persisted atomically.
- Step failure + task failure is persisted atomically.
- MCP unknown-outcome failures are explicitly classified as UNKNOWN and are never retried.
- Task step numbers are unique per task.
- Removed runtime sys.path mutation from package modules.

## r10 normalization
- `config/policy.txt` now matches runtime Policy: `backup` is VALIDATED, not AUTO.
- `config/runner.txt` now states that execution is implemented while the live MCP gate remains mandatory.


## r11 (2026-09-13)

- MCP Worker now records each dispatched MCP call in Agent-Next StateStore with task linkage and SUCCESS/FAILED/UNKNOWN_OUTCOME status.
- MCP JSON-RPC responses must declare `jsonrpc: 2.0` and match the request id.
- No automatic replay of `tools/call`; uncertain mutation outcomes remain non-retryable.
- Profile enforcement was intentionally not added: Policy remains the single capability authorization boundary until profile semantics are specified and tested end-to-end.


## r13 (2026-09-13)
- Confirmation resume is restart-safe across new StateStore/Executor instances.
- BLOCKED_ON_USER_ACTION rejects unconfirmed authorization; only a consumed challenge can resume it.
- StateStore schema constant is aligned with schema version 4.

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
- Secret-pattern scan: 0 hits.
- No live Agent Next, MCP Core/Bridge, or production process was modified while producing this archive.

## Additional clean-pass corrections r15-clean
- Valid MCP JSON-RPC errors are now represented by `MCPRPCError` and are not mislabeled as `UNKNOWN_OUTCOME`; transport/connection failures remain non-retryable unknown outcomes.
- Removed test-module `sys.path` mutations; only the deterministic test harness sets its own import path.
- Updated legacy-Agent documentation to reflect retirement after verification; the legacy Agent must not be recreated.
- Added regression coverage for the known JSON-RPC error vs unknown-outcome distinction and strengthened archive-integrity checks.


## Final archive verification r15-final (2026-09-13)
- Re-audited from a fresh extraction of the candidate archive.
- `python -m compileall -q app tests`: PASS.
- `tests/run_all.py`: PASS, `ALL_LOCAL_TESTS_OK`.
- All standalone runners used by the archive were also executed directly from the extracted root; PASS through Python module execution (`python -m`), keeping package imports path-independent.
- Direct function suites: PASS.
- Recovery, confirmation atomicity/persistence, lifecycle atomicity, executor restart, MCP audit/RPC, registry, worker, and policy probes: PASS.
- Archive contains no `__pycache__` or `.pyc`; no secret/credential filenames; no runtime SQLite state.
- Live Agent Next, existing MCP Core/Bridge, TNNL, and production were not modified during this archive repair.
- This archive is the single consolidated source snapshot for this repair cycle; previous intermediate r15 archives are superseded.


## Final source audit clarification r15-final
- Capability registry contains 57 unique tools. All 57 are covered by Policy; `run` is intentionally not exposed by the registry, while both `run` and `session_run` are hard-blocked.
- MCP topology is single-stack: Agent Next -> existing Bridge `127.0.0.1:18081/mcp` -> existing Native MCP Core `127.0.0.1:8081` -> existing transport/capabilities.
- This archive is source/install material only; live Bridge/Core acceptance remains a separate gate after clean installation.

## r16 packaging hardening (2026-09-13)
- The shared MCP/Core/Bridge/TNNL `ops/` stack is external to the Agent Next package and is omitted entirely. Live runtime PID/log state is never part of the source archive; `ops/` and top-level `logs/` are omitted from the distribution.
- Added `INSTALL.sh`. Default mode refuses overwrite; `--replace` creates a timestamped HOME backup and replaces only Agent Next source/config/tests/registry while preserving the external shared MCP/Core/Bridge/TNNL stack and existing runtime state files.
- The policy repair restores operation-aware composite authorization for `image_process`, `cloud_sync`, and `recipe_run`; Core-CONFIRM capabilities cannot be downgraded by `validated=True`.
- Packaging excludes generated bytecode, `__pycache__`, runtime PID/log state, `.pre-*` backup copies, development backup snapshots, and runtime databases.
- Clean-package checks are performed from a fresh extraction; the archive is source/install material only and live MCP acceptance remains a separate gate.


## Entrypoint repair — 2026-09-14
- Restored the missing Agent Next command entrypoint as `agent.py`.
- Restored the single local verification entrypoint as `verify.sh`.
- Updated `INSTALL.sh` so `--replace` installs both files; this closes the earlier installer omission where the package could contain the entrypoints but the target would not receive them.
- CLI commands are deterministic and policy-gated; no arbitrary shell/run/session_run execution is exposed.
- This repair does not create or replace MCP/Core/Bridge/TNNL infrastructure.


### 2026-09-14 launcher hardening
- Native MCP Core launcher now starts from `$HOME` and explicitly supplies HOME/PATH/TMPDIR/PYTHONPATH.
- This is a source-side runtime hardening change intended to eliminate CWD/environment-dependent `ENOENT` conditions observed in the live Core path.
- The existing single MCP/Core/Bridge/TNNL topology is unchanged.
- Live acceptance remains a separate gate because this archive cannot execute the user's Android/Termux runtime.


## Stage 3.1 checkpoint

Stage 3.1 exposes the Master Workflow orchestration through the deterministic
Agent Next CLI. Master creation, Stage1 child registration, dependency gating,
status persistence, reconciliation, completion, and failure propagation were
verified in an isolated test environment.

Local verification:
- 21/21 local test files passed.
- compileall passed.
- CLI contract test passed.
- Configuration safety audit passed.
- No credential values are packaged.
- Live MCP acceptance remains a separate post-install gate.

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

Stage 4 source snapshot includes deterministic master-plan orchestration for
eight Stage 1 child workflows.

Verified:
- 1 master workflow;
- 8 child workflows;
- 8 Stage 1 steps per child;
- 64 total child steps;
- explicit dependencies;
- deterministic workflow construction;
- atomic rollback on partial registration failure;
- invalid profile rejection;
- planning performs no workflow execution and no MCP calls.

Verification results:
- `tests/test_stage4_orchestrator.py`: 6/6 passed;
- `python3 -m tests.run_all`: `ALL_LOCAL_TESTS_OK`;
- full runner exit code: 0;
- Stage 1 and Stage 2 regression coverage remained green;
- `PRODUCTION_MUTATION=NONE`.

This Stage 4 source checkpoint is separate from live MCP acceptance.
The archive is source/install material and must be verified from a clean
extraction before being treated as a final archive.

## 2026-09-24 — Finalization checkpoint

- `VERIFY_BACKUP` dispatch was corrected to use the persisted BACKUP artifact verification path and must not dispatch a second MCP backup call.
- The dedicated `VERIFY_BACKUP` path retains the required `VALIDATED` authorization gate.
- Stage 1 regression coverage includes the persisted-backup-output dispatch boundary and the validation contract.
- Final full local regression: `ALL_LOCAL_TESTS_OK`, exit code 0.
- Final compile gate: `COMPILEALL_RC=0`.
- Root `test-backup.tar.gz` was identified as a 15-byte test marker rather than a gzip archive and was removed because packaging rules explicitly exclude this tiny development artifact.
- No production ReelForge mutation occurred.

Final source hashes:
- `app/workflow.py`: `e7ee79ba259ced8ec77c66365c4059a0043ab49030c1445d7e3dfd9c5d9a4361`
- `app/executor.py`: `c8be9a36d30cab15d84b05f7ad39c553cba80cbca8a1c107b6a609854f19d753`
- `app/state_store.py`: `e4a937e070de8bdc1501f0fba6abe39fe5b96e4599a95375e02c8bf58b5a336d`
- `app/path_resolver.py`: `995c4a17de432cf0e97913a871ccd572c062859a8b0ca7c566d73d697ca3f69d`
- `tests/test_stage1_workflow.py`: `13ae9a2a53d5b0ffac14d7c052811df07f193b0959c7d54b18cea649d7e79e4b`
- `00_LIVE_JOURNAL.md`: `732e7a20142723de10196dece32700908fd6ffbe6bcabe29e5814f26d4d76a1f`

Current archive status:
`FINAL SOURCE — LOCAL + LIVE ACCEPTANCE GATES GREEN; CLEAN EXTRACTION/INSTALL VERIFICATION GREEN`

`PRODUCTION_MUTATION=NONE`

## 2026-09-24 — Clean extraction final gate — GREEN

The archive `reelforge-agent-next-FINAL-20260924.tar.gz` was extracted into a fresh clean-check directory and independently verified.

Results:
- required installation/source files: GREEN;
- forbidden runtime/development artifacts: GREEN;
- `python3 -m compileall -q app tests`: RC 0;
- complete `tests/run_all.py`: `ALL_LOCAL_TESTS_OK`;
- clean-extraction test RC: 0;
- Stage 1: 16/16;
- Stage 4 orchestrator: 8/8;
- target-root resolver: 10/10;
- target-root executor confirmation: 1/1.

The clean extraction therefore reproduces the source package and passes the full local verification suite.

The archive SHA-256 is recorded externally immediately after the final archive build.

`FINAL_CLEAN_EXTRACTION_GATE=GREEN`
`MCP_DISPATCH=NONE`
`PRODUCTION_MUTATION=NONE`

## 2026-09-24 — FINAL R2 CLEAN EXTRACTION GATE — GREEN

The rebuilt archive `reelforge-agent-next-FINAL-20260924-r2.tar.gz` was extracted into a fresh clean-check directory and independently verified.

Results:
- required files: GREEN;
- forbidden runtime/development artifacts: GREEN;
- `python3 -m compileall -q app tests`: RC 0;
- complete `tests/run_all.py`: `ALL_LOCAL_TESTS_OK`;
- Stage 1: 16/16;
- Stage 4 orchestrator: 8/8;
- target-root resolver: 10/10;
- target-root executor confirmation: 1/1;
- full test RC: 0.

`FINAL_R2_CLEAN_EXTRACTION_GATE=GREEN`
`MCP_DISPATCH=NONE`
`PRODUCTION_MUTATION=NONE`
