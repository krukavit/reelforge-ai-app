import json
import os
from pathlib import Path

from .runner import Runner
from .mcp_worker import MCPWorker
from .mcp_client import MCPUnknownOutcomeError
from .confirmation import create_confirmation, consume_confirmation
from .backup_retention import BackupRetention
from .path_resolver import TargetRootResolver


class TaskExecutor:
    # Explicit filesystem-path fields whose meaning is relative to a task
    # target root. Special system-level capabilities such as backup/restore/
    # cloud_sync are intentionally excluded.
    _TARGET_ROOT_PATH_FIELDS = {
        "ls": {"path": True},
        "read": {"path": True},
        "write": {"path": False},
        "mkdir": {"path": False},
        "delete": {"path": True},
        "search": {"path": True},
        "diff": {"file": True, "file2": True},
        "git_status": {"repo_dir": True},
        "image_process": {"input": True, "output": False},
        "text_extract": {"input": True},
        "share": {"file": True},
        "screenshot": {"output": False},
        "camera_photo": {"output": False},
        "qrcode": {"output": False},
    }

    def __init__(self, state=None, worker=None):
        self.runner = Runner(state=state)
        self.worker = worker or MCPWorker(state=self.runner.state)
        self.backup_retention = BackupRetention(
            self.runner.state,
            max_per_target=int(os.environ.get("AGENT_NEXT_BACKUP_MAX_PER_TARGET", "5")),
        )

    @classmethod
    def _resolve_target_root_arguments(cls, task, capability, arguments):
        """Canonicalize project-relative filesystem arguments for a persisted task root."""
        call_arguments = dict(arguments or {})
        target_root = task.get("target_root") if task else None

        fields = cls._TARGET_ROOT_PATH_FIELDS.get(capability)
        if not target_root:
            # Core executes path arguments directly and does not shell-expand "~".
            # Canonicalize only explicit home-relative paths; ordinary relative
            # paths retain the Core's existing HOME semantics.
            if fields:
                home = os.environ.get("HOME", os.path.expanduser("~"))
                for field in fields:
                    value = call_arguments.get(field)
                    if isinstance(value, str) and value.startswith("~/"):
                        call_arguments[field] = os.path.join(home, value[2:])
            return call_arguments

        if not fields:
            return call_arguments

        resolver = TargetRootResolver(target_root)

        # Core defaults these two path-bearing capabilities to its HOME/cwd.
        # With a task root, omitted path means the task root instead.
        if capability in {"ls", "search"} and not call_arguments.get("path"):
            call_arguments["path"] = "."

        for field, must_exist in fields.items():
            if field not in call_arguments:
                continue
            value = call_arguments[field]
            if value is None or value == "":
                continue
            call_arguments[field] = str(
                resolver.resolve(value, must_exist=must_exist)
            )

        return call_arguments

    @staticmethod
    def _tool_result_error(capability, result):
        """Detect a known tool-level failure inside an otherwise valid MCP result."""
        if not isinstance(result, dict):
            return None

        # MCP tool results may explicitly mark a failed tool invocation.
        payload = result.get("result")
        if isinstance(payload, dict) and payload.get("isError") is True:
            return "MCP_TOOL_ERROR"

        # Current Core returns filesystem failures as plain text sentinels
        # inside an otherwise successful JSON-RPC response.
        content = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if not isinstance(text, str):
                    continue
                text = text.strip()

                if capability == "ls" and text.startswith("Cannot access:"):
                    return text

                if capability == "read" and text.startswith("Cannot read:"):
                    return text

        return None

    @staticmethod
    def _normalize_backup_path(backup_path):
        raw = str(backup_path)
        home = os.environ.get("HOME", os.path.expanduser("~"))
        if raw.startswith("~/"):
            resolved = os.path.join(home, raw[2:])
        elif not os.path.isabs(raw):
            resolved = os.path.join(home, raw)
        else:
            resolved = raw
        return os.path.normpath(resolved)

    @staticmethod
    def _verify_backup_artifact(backup_path):
        # Зеркалим резолюцию путей самого Core (handlers/terminal.py
        # handle_backup): относительный путь без "~/" резолвится
        # относительно $HOME, а НЕ относительно cwd процесса agent.py -
        # иначе проверка смотрит не туда, куда реально писал Core.
        path = Path(TaskExecutor._normalize_backup_path(backup_path))
        if not path.exists():
            return False, "BACKUP_MISSING"
        if path.is_symlink():
            return False, "BACKUP_SYMLINK"
        if not path.is_file():
            return False, "BACKUP_NOT_FILE"
        if path.stat().st_size <= 0:
            return False, "BACKUP_EMPTY"
        return True, "BACKUP_VERIFIED"

    def execute(self, task_id, capability, arguments=None, operation=None,
                profile="READ_ONLY", validated=False, target_root=None):
        # ВАЖНО: параметра confirmed здесь больше нет.
        # confirmed=True можно получить только через confirm_and_resume()
        # с реально пройденным challenge-токеном - никто (включая LLM-слой)
        # не может просто заявить "подтверждено" на входе в execute().
        self.runner.create_task(
            task_id,
            profile=profile,
            target_root=target_root,
        )
        task = self.runner.load_task(task_id)
        resolved_arguments = self._resolve_target_root_arguments(
            task, capability, arguments
        )
        self.runner.plan(task_id)
        step_id = self.runner.add_step(task_id, 1, capability)

        allowed, reason = self.runner.authorize_action(
            task_id, step_id, capability, operation,
            validated=validated, confirmed=False,
            arguments=resolved_arguments,
        )
        if not allowed:
            task = self.runner.load_task(task_id)
            out = {"task_id": task_id, "state": task["status"], "allowed": False, "reason": reason}
            if reason == "CONFIRM_REQUIRED":
                token = create_confirmation(
                    self.runner.state, task_id, capability,
                    operation=operation, step_no=1,
                    arguments=resolved_arguments, step_id=step_id,
                )
                out["confirmation_token"] = token
                out["confirmation_note"] = (
                    "Одноразовый токен. Передайте его в confirm_and_resume() "
                    "именно для этого task_id, чтобы выполнить действие."
                )
            return out

        return self._run_step(
            task_id, step_id, capability, resolved_arguments, operation, reason,
            validated=validated,
        )

    def confirm_and_resume(self, task_id, confirmation_token, arguments=None):
        task = self.runner.load_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        if task["status"] != "BLOCKED_ON_USER_ACTION":
            return {"task_id": task_id, "state": task["status"], "allowed": False, "reason": "NOT_AWAITING_CONFIRMATION"}

        steps = self.runner.load_steps(task_id)
        if not steps:
            raise RuntimeError(f"Task {task_id} has no steps to confirm")
        step = steps[-1]
        capability = step["name"]
        operation = None
        auth = step["authorization"]
        if isinstance(auth, str):
            try:
                auth = json.loads(auth)
            except Exception:
                auth = {}
        if not isinstance(auth, dict):
            auth = {}

        operation = auth.get("operation")

        if arguments is None:
            arguments = auth.get("arguments")

        resolved_arguments = self._resolve_target_root_arguments(
            task, capability, arguments
        )

        ok, reason = consume_confirmation(
            self.runner.state, task_id, capability, confirmation_token,
            operation=operation, arguments=resolved_arguments, step_id=step["id"],
        )
        if not ok:
            # Ключевая проверка: НЕ доверяем никакому другому "подтверждению",
            # кроме прошедшего именно через этот механизм.
            return {"task_id": task_id, "state": task["status"], "allowed": False, "reason": reason}

        # A confirmed task is persisted in BLOCKED_ON_USER_ACTION. Re-authorize
        # it there using the consumed challenge, then resume execution.
        allowed, policy_reason = self.runner.authorize_action(
            task_id, step["id"], capability, operation,
            validated=False, confirmed=True,
            arguments=resolved_arguments,
        )
        if not allowed:
            current = self.runner.load_task(task_id)
            return {"task_id": task_id, "state": current["status"], "allowed": False, "reason": policy_reason}

        self.runner.transition(task_id, "RUNNING")
        return self._run_step(
            task_id, step["id"], capability, resolved_arguments, operation,
            policy_reason, confirmed=True,
        )

    def _run_step(self, task_id, step_id, capability, arguments, operation, reason,
                  validated=False, confirmed=False, verify_backup=True):
        self.runner.transition(task_id, "RUNNING")
        task = self.runner.load_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")

        call_arguments = self._resolve_target_root_arguments(
            task, capability, arguments
        )
        if confirmed:
            # Core сам по себе ожидает confirmed:true внутри arguments
            # самого tool call, отдельно от нашей Policy-проверки.
            # Кладём это только здесь, ПОСЛЕ того как challenge уже
            # реально пройден через consume_confirmation() выше по стеку.
            call_arguments["confirmed"] = True
        try:
            result = self.worker.call_tool(
                capability, call_arguments, operation=operation,
                validated=validated, confirmed=confirmed, task_id=task_id,
            )
            tool_error = self._tool_result_error(capability, result)
            if tool_error is not None:
                self.runner.state.fail_step_and_task(
                    task_id, step_id, tool_error, recovery_reason="TOOL_FAILURE"
                )
                self.runner.state.add_event(
                    "executor", "TOOL_FAILURE", task_id,
                    {"step_id": step_id, "capability": capability, "error": tool_error},
                )
                return {
                    "task_id": task_id,
                    "state": "FAILED",
                    "allowed": True,
                    "reason": "TOOL_FAILURE",
                    "error": tool_error,
                    "result": result,
                }

            if capability == "backup" and isinstance(result, dict):
                # The live Core backup response may omit target/output metadata.
                # Preserve the Agent Next workflow arguments in the persisted
                # step result so VERIFY_BACKUP cannot silently fall back to home.
                result = dict(result)
                requested_output = call_arguments.get("output")
                if requested_output:
                    requested_norm = self._normalize_backup_path(requested_output)
                    for key in ("backup_path", "output"):
                        reported = result.get(key)
                        if reported and self._normalize_backup_path(reported) != requested_norm:
                            error = "BACKUP_PATH_MISMATCH"
                            self.runner.state.fail_step_and_task(
                                task_id, step_id, error,
                                recovery_reason="BACKUP_PATH_MISMATCH",
                            )
                            self.runner.state.add_event(
                                "executor", "BACKUP_PATH_MISMATCH", task_id,
                                {
                                    "step_id": step_id,
                                    "requested_output": requested_output,
                                    "reported_key": key,
                                    "reported_path": reported,
                                },
                            )
                            return {
                                "task_id": task_id,
                                "state": "FAILED",
                                "allowed": True,
                                "reason": "BACKUP_PATH_MISMATCH",
                                "error": error,
                            }
                result.setdefault("target", call_arguments.get("target", "home"))
                if requested_output:
                    result.setdefault("output", requested_output)
            retention = None
            if capability == "backup" and verify_backup:
                target = call_arguments.get("target", "home")
                backup_path = call_arguments.get("output")
                if not backup_path and isinstance(result, dict):
                    backup_path = result.get("backup_path") or result.get("output")

                verified, verify_reason = self._verify_backup_artifact(backup_path) if backup_path else (False, "BACKUP_PATH_MISSING")

                self.runner.state.add_test(
                    "BACKUP_VERIFY",
                    "PASSED" if verified else "FAILED",
                    details={"path": backup_path, "reason": verify_reason},
                    task_id=task_id,
                )

                if not verified:
                    error = f"Backup artifact verification failed: {verify_reason}"
                    self.runner.state.fail_step_and_task(
                        task_id, step_id, error, recovery_reason="BACKUP_VERIFY_FAILED"
                    )
                    self.runner.state.add_event(
                        "executor",
                        "BACKUP_VERIFY_FAILED",
                        task_id,
                        {"step_id": step_id, "path": backup_path, "reason": verify_reason},
                    )
                    return {
                        "task_id": task_id,
                        "state": "FAILED",
                        "allowed": True,
                        "reason": "BACKUP_VERIFY_FAILED",
                        "error": error,
                    }

                self.runner.state.add_backup(target, backup_path, "SUCCESS", task_id=task_id)
                retention = self.backup_retention.prune(target)
            task_after, _ = self.runner.state.complete_step_and_task(task_id, step_id, result=result)
            if task_after["status"] == "COMPLETED":
                self.runner.state.add_event("executor", "TASK_COMPLETED", task_id, {"step_id": step_id})
            else:
                self.runner.state.add_event("executor", "STEP_COMPLETED", task_id, {"step_id": step_id})
            out = {"task_id": task_id, "state": task_after["status"], "allowed": True, "reason": reason, "result": result}
            if retention is not None:
                out["backup_retention"] = retention
            return out
        except MCPUnknownOutcomeError as exc:
            self.runner.state.fail_step_and_task(
                task_id, step_id, str(exc), recovery_reason="UNKNOWN"
            )
            self.runner.state.add_event("executor", "UNKNOWN_OUTCOME", task_id, {"step_id": step_id})
            return {"task_id": task_id, "state": "FAILED", "allowed": True, "reason": "UNKNOWN_OUTCOME", "error": str(exc)}
        except Exception as exc:
            self.runner.state.fail_step_and_task(task_id, step_id, str(exc))
            self.runner.state.add_event("executor", "TASK_FAILED", task_id, {"step_id": step_id})
            return {"task_id": task_id, "state": "FAILED", "allowed": True, "reason": reason, "error": str(exc)}
