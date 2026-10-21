#!/usr/bin/env python3
"""ReelForge Agent Next command entrypoint.

This is the deterministic local control surface for Agent Next. It delegates
execution to TaskExecutor/Runner/Policy/MCPWorker and never exposes arbitrary
shell execution. `run` and `session_run` remain blocked by the runtime policy.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.capability_registry import CapabilityRegistry
from app.executor import TaskExecutor
from app.recovery import find_stuck_tasks, recover_stuck_tasks
from app.state_store import StateStore
from app.policy import authorize
from app.workflow import WorkflowExecutor
from app.master_workflow import MasterWorkflowExecutor
from app.orchestrator import AgentOrchestrator
from app.conversation_service import ConversationService
from app.conversation_response import format_conversation_result
from app.voice_service import VoiceService
from app.voice_loop import VoiceLoop

from app.memory_guardian import inspect as inspect_memory_guardian
from app.local_test_runner import ALLOWED_TESTS, LocalTestRunner
from app.diagnostic_report import diagnose_result
from app.fix_proposal import create_proposal


def startup_memory_guard():
    """Conservatively protect RAM before chat or voice processing."""
    try:
        result = inspect_memory_guardian(allow_stop=False)
        status = result.get("status", "UNKNOWN")
        reason = result.get("reason", "UNKNOWN")
        available = result.get("available_mib")
        stopped = result.get("stopped_package")
        print(
            f"[RAM-GUARD] status={status} reason={reason} "
            f"available_mib={available} stopped={stopped}",
            file=sys.stderr,
        )
    except Exception:
        # Guardian errors must never prevent the agent from starting.
        print("[RAM-GUARD] status=SAFE_SKIP reason=GUARD_ERROR", file=sys.stderr)




def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def make_task_id(prefix="agent-next"):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def cmd_self_test():
    store = StateStore()
    registry = CapabilityRegistry()
    reg = registry.validate()
    for blocked in ("run", "session_run"):
        allowed, reason = authorize(blocked)
        if allowed or reason != "ALWAYS_BLOCKED":
            raise RuntimeError(f"policy leak for {blocked}: {allowed}/{reason}")
    emit({
        "status": "AGENT_NEXT_SELF_TEST_OK",
        "project_root": str(ROOT),
        "db": str(store.path),
        "schema_version": store.get_schema_version(),
        "registry": reg,
    })


def cmd_health():
    result = TaskExecutor().execute(make_task_id("health"), "health", arguments={})
    emit(result)
    if result.get("state") != "COMPLETED":
        raise SystemExit(1)


def cmd_registry():
    registry = CapabilityRegistry()
    data = registry.load()
    emit(registry.validate() | {"tools": registry.names()})


def cmd_status():
    store = StateStore()
    with store._connect() as con:
        tasks = [dict(r) for r in con.execute(
            "SELECT id, master_task, profile, status, stage, created_at, finished_at, error, recovery_reason "
            "FROM tasks ORDER BY created_at DESC LIMIT 20"
        ).fetchall()]
        workers = [dict(r) for r in con.execute(
            "SELECT id, task_id, status, pid, started_at, finished_at, heartbeat_at "
            "FROM workers ORDER BY started_at DESC LIMIT 20"
        ).fetchall()]
    emit({
        "status": "OK",
        "db": str(store.path),
        "schema_version": store.get_schema_version(),
        "stuck_tasks": [t["id"] for t in find_stuck_tasks(store)],
        "tasks": tasks,
        "workers": workers,
    })


def cmd_recover(reason):
    store = StateStore()
    before = [t["id"] for t in find_stuck_tasks(store)]
    recovered = recover_stuck_tasks(store, reason=reason)
    emit({"status": "RECOVERY_OK", "reason": reason, "found": before, "recovered": recovered})


def parse_args_json(raw):
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON arguments: {exc}")
    if not isinstance(value, dict):
        raise SystemExit("arguments must be a JSON object")
    return value


def cmd_tool(args):
    if args.tool_name in {"run", "session_run"}:
        raise SystemExit(f"BLOCKED_BY_POLICY: {args.tool_name}")
    arguments = parse_args_json(args.arguments)
    task_id = args.task_id or make_task_id(args.tool_name)
    result = TaskExecutor().execute(
        task_id,
        args.tool_name,
        arguments=arguments,
        operation=args.operation,
        profile=args.profile,
        validated=args.validated,
    )
    emit(result)
    if result.get("state") == "FAILED":
        raise SystemExit(1)


def cmd_chat(args):
    startup_memory_guard()
    result = ConversationService().handle(
        args.text,
        target_root=args.target_root,
    )
    print(format_conversation_result(result))
    if result.get("result", {}).get("state") == "FAILED":
        raise SystemExit(1)


def cmd_voice(args):
    startup_memory_guard()
    result = VoiceService().listen_and_handle(
        target_root=args.target_root,
    )
    emit(result)

    if result.get("state") == "STT_EMPTY":
        raise SystemExit(1)

    if result.get("conversation", {}).get("result", {}).get("state") == "FAILED":
        raise SystemExit(1)


def cmd_voice_loop(args):
    startup_memory_guard()
    VoiceLoop(pause_seconds=args.pause).run(
        target_root=args.target_root,
    )


def cmd_confirm(args):
    arguments = parse_args_json(args.arguments) if args.arguments is not None else None
    result = TaskExecutor().confirm_and_resume(
        args.task_id, args.confirmation_token, arguments=arguments
    )
    emit(result)
    if result.get("state") == "FAILED":
        raise SystemExit(1)


def cmd_orchestrate(args):
    """Build and atomically register a master Stage1 plan.

    R12 is planning/registration only. It must not execute MCP capabilities.
    """
    orchestrator = AgentOrchestrator()

    plan = orchestrator.build_plan(
        args.description,
        profile=args.profile,
        target_root=args.target_root,
    )

    result = orchestrator.create_from_plan(plan)

    emit({
        "status": "ORCHESTRATION_REGISTERED",
        "execution_started": result["execution_started"],
        "mcp_calls": result["mcp_calls"],
        "master": result["master"],
        "children": result["children"],
    })

def cmd_master_create(args):
    master = MasterWorkflowExecutor()
    task_id = args.task_id or make_task_id("master")
    emit(master.create_master(task_id))


def cmd_master_add_stage1(args):
    master = MasterWorkflowExecutor()
    dependencies = []
    if args.depends_on:
        dependencies = [item for item in args.depends_on.split(",") if item]
    emit(master.add_stage1(
        args.master_task_id,
        args.child_task_id,
        profile=args.profile,
        depends_on=dependencies,
    ))


def cmd_master_start_child(args):
    master = MasterWorkflowExecutor()
    emit(master.start_child(
        args.master_task_id,
        args.child_task_id,
    ))


def cmd_master_status(args):
    master = MasterWorkflowExecutor()
    emit(master.status(args.master_task_id))


def cmd_master_reconcile(args):
    master = MasterWorkflowExecutor()
    emit(master.reconcile(args.master_task_id))


def cmd_workflow_create(args):
    wf = WorkflowExecutor()
    task_id = args.task_id or make_task_id("workflow")
    result = wf.create_stage1(task_id, profile=args.profile)
    emit(result)


def cmd_workflow_step(args):
    wf = WorkflowExecutor()
    arguments = parse_args_json(args.arguments)
    result = wf.execute_step(
        args.task_id, args.step_name, args.capability,
        arguments=arguments, operation=args.operation,
        validated=args.validated, reason=args.reason,
    )
    emit(result)
    if result.get("state") == "FAILED" or result.get("allowed") is False:
        raise SystemExit(1)


def cmd_workflow_confirm_step(args):
    wf = WorkflowExecutor()
    arguments = parse_args_json(args.arguments) if args.arguments is not None else None
    result = wf.confirm_and_execute_step(
        args.task_id, args.step_name, args.capability,
        args.confirmation_token, arguments=arguments, operation=args.operation,
    )
    emit(result)
    if result.get("state") == "FAILED" or result.get("allowed") is False:
        raise SystemExit(1)


def cmd_workflow_verify_backup(args):
    wf = WorkflowExecutor()
    result = wf.verify_backup(args.task_id, target=args.target, backup_path=args.output)
    emit(result)
    if result.get("state") == "FAILED":
        raise SystemExit(1)


def cmd_workflow_status(args):
    wf = WorkflowExecutor()
    emit(wf.load(args.task_id))



def cmd_chat_confirm(args):
    service = ConversationService()
    arguments = json.loads(args.arguments) if args.arguments else None
    try:
        result = service.confirm(
            args.task_id,
            args.confirmation_token,
            arguments=arguments,
        )
    except (KeyError, ValueError, RuntimeError) as exc:
        print(format_conversation_result({"error": str(exc)}))
        return 1

    print(format_conversation_result({
        "task_id": result.get("task_id"),
        "result": result,
    }))
    return 1 if result.get("allowed") is False or result.get("state") == "FAILED" else 0



def cmd_local_check(args):
    runner = LocalTestRunner(args.target_root, timeout_seconds=args.timeout)
    if args.mode == "compile":
        if args.test_name is not None:
            raise SystemExit("--test-name is only valid with --mode test")
        result = runner.run_compile()
    else:
        if args.test_name is None:
            raise SystemExit("--test-name is required with --mode test")
        result = runner.run_test(args.test_name)

    report = diagnose_result(result)
    emit({
        "status": "PASSED" if result.passed else "FAILED",
        "result": result.as_dict(),
        "diagnostic": report,
        "execution_allowed": False,
        "changes_applied": False,
    })
    if not result.passed:
        raise SystemExit(1)


def cmd_fix_proposal(args):
    result = create_proposal(
        target_root=args.target_root,
        category=args.category,
        file_path=args.file_path,
        evidence=args.evidence,
        rationale=args.rationale,
        expected_effect=args.expected_effect,
        verification=args.verification,
    )
    emit(result)
    if result.get("status") not in ("PROPOSAL_READY", "NO_FIX_NEEDED"):
        raise SystemExit(1)

def build_parser():
    p = argparse.ArgumentParser(description="ReelForge Agent Next")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    sub.add_parser("health")
    sub.add_parser("registry")
    sub.add_parser("status")
    rec = sub.add_parser("recover")
    rec.add_argument("--reason", default="PROCESS_RESTART",
                     choices=["PROCESS_RESTART", "BRIDGE_RESTART", "CORE_RESTART",
                              "NETWORK_FAILURE", "USER_ACTION", "TOOL_FAILURE",
                              "POLICY_BLOCK", "UNKNOWN"])
    tool = sub.add_parser("tool")
    tool.add_argument("tool_name")
    tool.add_argument("arguments", nargs="?", default="{}")
    tool.add_argument("--operation")
    tool.add_argument("--task-id")
    tool.add_argument("--profile", default="READ_ONLY")
    tool.add_argument("--validated", action="store_true")
    orchestrate = sub.add_parser(
        "orchestrate",
        help="Build and register a Stage1 master workflow without executing MCP",
    )
    orchestrate.add_argument("description")
    orchestrate.add_argument("--profile", default="EDIT")
    orchestrate.add_argument("--target-root")

    master_create = sub.add_parser("master-create")
    master_create.add_argument("--task-id")

    master_add = sub.add_parser("master-add-stage1")
    master_add.add_argument("master_task_id")
    master_add.add_argument("child_task_id")
    master_add.add_argument("--profile", default="EDIT")
    master_add.add_argument("--depends-on")

    master_start = sub.add_parser("master-start-child")
    master_start.add_argument("master_task_id")
    master_start.add_argument("child_task_id")

    master_status = sub.add_parser("master-status")
    master_status.add_argument("master_task_id")

    master_reconcile = sub.add_parser("master-reconcile")
    master_reconcile.add_argument("master_task_id")

    wf_create = sub.add_parser("workflow-create")
    wf_create.add_argument("--task-id")
    wf_create.add_argument("--profile", default="EDIT")

    wf_step = sub.add_parser("workflow-step")
    wf_step.add_argument("task_id")
    wf_step.add_argument("step_name", choices=list(__import__("app.workflow", fromlist=["STAGE1_STEPS"]).STAGE1_STEPS))
    wf_step.add_argument("capability")
    wf_step.add_argument("arguments", nargs="?", default="{}")
    wf_step.add_argument("--operation")
    wf_step.add_argument("--validated", action="store_true")
    wf_step.add_argument("--reason")

    wf_confirm = sub.add_parser("workflow-confirm-step")
    wf_confirm.add_argument("task_id")
    wf_confirm.add_argument("step_name")
    wf_confirm.add_argument("capability")
    wf_confirm.add_argument("confirmation_token")
    wf_confirm.add_argument("--arguments")
    wf_confirm.add_argument("--operation")

    wf_verify = sub.add_parser("workflow-verify-backup")
    wf_verify.add_argument("task_id")
    wf_verify.add_argument("--target")
    wf_verify.add_argument("--output")

    wf_status = sub.add_parser("workflow-status")
    wf_status.add_argument("task_id")

    chat = sub.add_parser(
        "chat",
        help="Handle a natural-language Russian command through Agent Next",
    )
    chat.add_argument("text")
    chat.add_argument("--target-root")

    voice = sub.add_parser(
        "voice",
        help="Capture speech and route it through Agent Next",
    )
    voice.add_argument("--target-root")

    voice_loop = sub.add_parser(
        "voice-loop",
        help="Continuously listen, transcribe speech, and answer aloud",
    )
    voice_loop.add_argument("--target-root")
    voice_loop.add_argument("--pause", type=float, default=0.75)

    confirm = sub.add_parser("confirm")
    confirm.add_argument("task_id")
    confirm.add_argument("confirmation_token")
    confirm.add_argument("--arguments")

    chat_confirm = sub.add_parser("chat-confirm")
    chat_confirm.add_argument("task_id")
    chat_confirm.add_argument("confirmation_token")
    chat_confirm.add_argument("--arguments", required=True)
    local_check = sub.add_parser(
        "local-check",
        help="Run one explicitly selected bounded local verification",
    )
    local_check.add_argument("target_root")
    local_check.add_argument("--mode", choices=["compile", "test"], required=True)
    local_check.add_argument("--test-name", choices=sorted(ALLOWED_TESTS))
    local_check.add_argument("--timeout", type=int, default=30, choices=range(1, 61))

    proposal = sub.add_parser(
        "fix-proposal",
        help="Create an informational fix proposal without changing files",
    )
    proposal.add_argument("target_root")
    proposal.add_argument("category", choices=[
        "TEST_FAILURE", "COMPILE_FAILURE", "TIMEOUT", "UNKNOWN_OUTCOME"
    ])
    proposal.add_argument("file_path")
    proposal.add_argument("--evidence", required=True)
    proposal.add_argument("--rationale", required=True)
    proposal.add_argument("--expected-effect", required=True)
    proposal.add_argument("--verification", required=True)

    return p


def main():
    args = build_parser().parse_args()
    if args.command == "self-test": return cmd_self_test()
    if args.command == "health": return cmd_health()
    if args.command == "registry": return cmd_registry()
    if args.command == "status": return cmd_status()
    if args.command == "recover": return cmd_recover(args.reason)
    if args.command == "tool": return cmd_tool(args)
    if args.command == "orchestrate": return cmd_orchestrate(args)
    if args.command == "chat": return cmd_chat(args)
    if args.command == "voice": return cmd_voice(args)
    if args.command == "voice-loop": return cmd_voice_loop(args)
    if args.command == "chat-confirm": return cmd_chat_confirm(args)
    if args.command == "confirm": return cmd_confirm(args)
    if args.command == "master-create": return cmd_master_create(args)
    if args.command == "master-add-stage1": return cmd_master_add_stage1(args)
    if args.command == "master-start-child": return cmd_master_start_child(args)
    if args.command == "master-status": return cmd_master_status(args)
    if args.command == "master-reconcile": return cmd_master_reconcile(args)
    if args.command == "workflow-create": return cmd_workflow_create(args)
    if args.command == "workflow-step": return cmd_workflow_step(args)
    if args.command == "workflow-confirm-step": return cmd_workflow_confirm_step(args)
    if args.command == "workflow-verify-backup": return cmd_workflow_verify_backup(args)
    if args.command == "workflow-status": return cmd_workflow_status(args)
    if args.command == "local-check": return cmd_local_check(args)
    if args.command == "fix-proposal": return cmd_fix_proposal(args)
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
