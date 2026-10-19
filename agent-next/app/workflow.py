from __future__ import annotations

from .runner import Runner
from .executor import TaskExecutor
from .confirmation import consume_confirmation, create_confirmation
from .local_test_runner import ALLOWED_TESTS, LocalTestRunner
from .policy import authorize_local_workflow_step


STAGE1_STEPS = (
    "BACKUP",
    "VERIFY_BACKUP",
    "DIAGNOSE",
    "READ",
    "PLAN",
    "EDIT",
    "TEST",
    "VERIFY",
)


class WorkflowExecutor:
    """Stage 1 orchestration layer over the existing Runner/StateStore.

    This class does not replace TaskExecutor, Policy, MCPWorker, or Runner.
    It only owns the lifecycle of one multi-step workflow task.
    """

    def __init__(self, state=None):
        self.runner = Runner(state=state)

    @staticmethod
    def _decode_step_result(step):
        import json
        raw = step.get("result")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            return value if isinstance(value, dict) else {}
        return {}

    def create_stage1(
        self,
        task_id: str,
        profile: str = "EDIT",
        target_root=None,
    ):
        self.runner.create_task(
            task_id,
            master_task=task_id,
            profile=profile,
            target_root=target_root,
        )
        self.runner.plan(
            task_id,
            stage="STAGE1",
            checkpoint="CREATED",
        )

        steps = {}
        for number, name in enumerate(STAGE1_STEPS, 1):
            steps[name] = self.runner.add_step(task_id, number, name)

        self.runner.checkpoint(
            task_id,
            "WORKFLOW_CREATED",
            stage="STAGE1",
        )

        return {
            "task_id": task_id,
            "stage": "STAGE1",
            "status": "PLANNED",
            "steps": steps,
        }

    def load(self, task_id: str):
        return {
            "task": self.runner.load_task(task_id),
            "steps": self.runner.load_steps(task_id),
        }


    def execute_step(
        self,
        task_id: str,
        step_name: str,
        capability: str,
        arguments=None,
        operation=None,
        *,
        validated=False,
        confirmed=False,
        reason=None,
        worker=None,
    ):
        if confirmed:
            raise RuntimeError(
                "CONFIRMATION_REQUIRED_USE_WORKFLOW_CONFIRM_AND_EXECUTE"
            )

        if step_name not in {"TEST", "VERIFY"}:
            self.require_previous_step_completed(task_id, step_name)
        if step_name in {"TEST", "VERIFY"}:
            # Validate every replay request before treating it as idempotent.
            task = self.runner.load_task(task_id)
            steps = self.runner.load_steps(task_id)
            matches = [item for item in steps if item["name"] == step_name]
            if len(matches) != 1:
                raise RuntimeError("LOCAL_WORKFLOW_STEP_STRUCTURE_INVALID")

            step = matches[0]
            valid_arguments = isinstance(arguments, dict)

            if step_name == "TEST":
                mode = arguments.get("mode") if valid_arguments else None
                if mode == "compile":
                    valid_arguments = set(arguments) == {"mode"}
                elif mode == "test":
                    valid_arguments = (
                        set(arguments) == {"mode", "test_name"}
                        and isinstance(arguments.get("test_name"), str)
                        and arguments["test_name"] in ALLOWED_TESTS
                    )
                else:
                    valid_arguments = False
                expected_capability = "local_test"
                expected_operation = mode
            else:
                valid_arguments = valid_arguments and not arguments
                expected_capability = "local_verify"
                expected_operation = "persisted_result"

            allowed, policy_reason = authorize_local_workflow_step(
                step_name, capability, operation
            )
            if not valid_arguments:
                allowed, policy_reason = False, "LOCAL_STEP_ARGUMENTS_INVALID"
            elif validated:
                allowed, policy_reason = False, "UNEXPECTED_VALIDATION_FLAG"
            elif operation != expected_operation:
                allowed, policy_reason = False, "LOCAL_STEP_OPERATION_MISMATCH"

            if step["status"] == "COMPLETED":
                # A completed step is replayable only for a valid request and
                # an active or completed task. Never rerun local test commands.
                if not allowed:
                    return {
                        "task_id": task_id,
                        "step_id": step["id"],
                        "step": step_name,
                        "allowed": False,
                        "reason": policy_reason,
                    }

                if not task or task.get("status") not in {"RUNNING", "COMPLETED"}:
                    return {
                        "task_id": task_id,
                        "step_id": step["id"],
                        "step": step_name,
                        "state": task.get("status") if task else None,
                        "allowed": False,
                        "reason": "LOCAL_STEP_TASK_NOT_RUNNING",
                    }

                import json

                result = step.get("result")
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except (ValueError, TypeError):
                        pass

                return {
                    "task_id": task_id,
                    "step_id": step["id"],
                    "step": step_name,
                    "state": "COMPLETED",
                    "allowed": True,
                    "reason": "STEP_ALREADY_COMPLETED",
                    "result": result,
                }

            if not task or task.get("status") != "RUNNING":
                return {
                    "task_id": task_id,
                    "step_id": step["id"],
                    "step": step_name,
                    "state": task.get("status") if task else None,
                    "allowed": False,
                    "reason": "LOCAL_STEP_TASK_NOT_RUNNING",
                }

            # Reject invalid local requests before checking workflow sequence.
            # A denied request must not mutate step authorization state.
            if not allowed:
                return {
                    "task_id": task_id,
                    "step_id": step["id"],
                    "step": step_name,
                    "allowed": False,
                    "reason": policy_reason,
                }

            # Valid requests still must satisfy the workflow sequence.
            self.require_previous_step_completed(task_id, step_name)

            # Persist only fixed policy metadata; never persist arbitrary arguments.
            self.runner.state.update_step(
                step["id"],
                authorization={
                    "capability": capability,
                    "operation": operation,
                    "allowed": allowed,
                    "reason": policy_reason,
                    "validated": False,
                    "confirmed": False,
                    "local_workflow_policy": True,
                },
            )

            if step_name == "TEST":
                return self._execute_local_test(task_id, arguments=arguments)
            return self._verify_local_test(task_id)

        # VERIFY_BACKUP validates the artifact produced by the preceding
        # BACKUP step. It must not dispatch a second MCP backup call.
        # Use the dedicated verification path, which reads the persisted
        # BACKUP result and resolves its output path.
        if step_name == "VERIFY_BACKUP":
            # Preserve the workflow validation gate before entering the
            # dedicated persisted-artifact verification path.
            if not validated:
                return {
                    "task_id": task_id,
                    "step": step_name,
                    "allowed": False,
                    "reason": "VALIDATION_REQUIRED",
                }

            verify_arguments = arguments or {}
            return self.verify_backup(
                task_id,
                target=verify_arguments.get("target"),
                backup_path=verify_arguments.get("output"),
            )

        if step_name == "EDIT":
            self.require_verified_backup(task_id)

        steps = self.runner.load_steps(task_id)
        matches = [step for step in steps if step["name"] == step_name]
        if not matches:
            raise KeyError(f"Workflow step not found: {step_name}")

        step = matches[0]

        if step["status"] == "COMPLETED":
            result = step.get("result")
            if isinstance(result, str):
                try:
                    import json
                    result = json.loads(result)
                except Exception:
                    pass
            return {
                "task_id": task_id,
                "step_id": step["id"],
                "step": step_name,
                "state": "COMPLETED",
                "allowed": True,
                "reason": "STEP_ALREADY_COMPLETED",
                "result": result,
            }

        if step_name == "PLAN":
            task = self.runner.load_task(task_id)
            if task["status"] == "PLANNED":
                self.runner.transition(task_id, "RUNNING")
            elif task["status"] != "RUNNING":
                raise RuntimeError(
                    f"PLAN_INVALID_TASK_STATE:{task['status']}"
                )

            task_after, _ = self.runner.state.complete_step_and_task(
                task_id,
                step["id"],
                result=arguments,
            )

            self.runner.state.add_event(
                "workflow",
                "PLAN_COMPLETED",
                task_id,
                {"step_id": step["id"]},
            )

            return {
                "task_id": task_id,
                "step_id": step["id"],
                "step": step_name,
                "state": task_after["status"],
                "allowed": True,
                "reason": reason or "WORKFLOW_PLAN",
                "result": arguments,
            }

        task = self.runner.load_task(task_id)

        resolved_arguments = TaskExecutor._resolve_target_root_arguments(
            task,
            capability,
            arguments,
        )

        allowed, policy_reason = self.runner.authorize_action(
            task_id,
            step["id"],
            capability,
            operation,
            validated=validated,
            confirmed=False,
        )

        if not allowed:
            out = {
                "task_id": task_id,
                "step_id": step["id"],
                "step": step_name,
                "allowed": False,
                "reason": policy_reason,
            }

            if policy_reason == "CONFIRM_REQUIRED":
                token = create_confirmation(
                    self.runner.state,
                    task_id,
                    capability,
                    operation=operation,
                    step_no=step.get("step_no"),
                    arguments=resolved_arguments,
                    step_id=step["id"],
                )
                out["confirmation_token"] = token
                out["confirmation_note"] = (
                    "Одноразовый токен. Передайте его в "
                    "workflow-confirm-step именно для этого task_id и шага."
                )

            return out

        executor = TaskExecutor(
            state=self.runner.state,
            worker=worker,
        )

        return executor._run_step(
            task_id,
            step["id"],
            capability,
            arguments,
            operation,
            reason or "WORKFLOW",
            validated=validated,
            confirmed=confirmed,
            verify_backup=(step_name != "BACKUP"),
        )



    def _execute_local_test(self, task_id, *, arguments=None):
        """Run only a fixed compile command or an allowlisted local test."""
        from pathlib import Path

        if not isinstance(arguments, dict):
            return {
                "task_id": task_id,
                "step": "TEST",
                "allowed": False,
                "reason": "TEST_ARGUMENTS_INVALID",
            }

        mode = arguments.get("mode")
        if mode not in {"compile", "test"}:
            return {
                "task_id": task_id,
                "step": "TEST",
                "allowed": False,
                "reason": "TEST_MODE_REQUIRED",
            }

        task = self.runner.load_task(task_id)
        target_root = (
            task.get("target_root")
            or str(Path(__file__).resolve().parent.parent)
        )
        runner = LocalTestRunner(target_root)

        try:
            if mode == "compile":
                result = runner.run_compile()
            else:
                test_name = arguments.get("test_name")
                if not isinstance(test_name, str) or not test_name:
                    return {
                        "task_id": task_id,
                        "step": "TEST",
                        "allowed": False,
                        "reason": "ALLOWLISTED_TEST_NAME_REQUIRED",
                    }
                result = runner.run_test(test_name)
        except (ValueError, FileNotFoundError, OSError) as exc:
            return {
                "task_id": task_id,
                "step": "TEST",
                "allowed": False,
                "reason": "LOCAL_TEST_REJECTED",
                "error": "Local test rejected; details withheld.",
            }

        payload = result.as_dict()
        payload["passed"] = bool(result.passed)
        step_matches = [
            item for item in self.runner.load_steps(task_id)
            if item["name"] == "TEST"
        ]
        if len(step_matches) != 1:
            raise RuntimeError("TEST_STEP_NOT_UNIQUE")
        step = step_matches[0]

        status = "PASSED" if result.passed else "FAILED"
        self.runner.state.add_test(
            result.mode, status, details=payload, task_id=task_id
        )

        if not result.passed:
            reason = (
                "TIMEOUT" if result.timed_out else
                "COMPILE_FAILURE" if result.mode == "compile" else
                "TEST_FAILURE"
            )
            error = (
                f"{reason}: local verification did not pass "
                f"(returncode={result.returncode})"
            )
            self.runner.state.fail_step_and_task(
                task_id, step["id"], error,
                recovery_reason="TOOL_FAILURE",
            )
            self.runner.state.add_event(
                "workflow", "LOCAL_TEST_FAILED", task_id,
                {"mode": result.mode, "returncode": result.returncode,
                 "timed_out": result.timed_out},
            )
            return {
                "task_id": task_id,
                "step_id": step["id"],
                "step": "TEST",
                "state": "FAILED",
                "allowed": True,
                "reason": reason,
                "result": payload,
            }

        task_after, _ = self.runner.state.complete_step_and_task(
            task_id, step["id"], result=payload
        )
        self.runner.state.add_event(
            "workflow", "LOCAL_TEST_PASSED", task_id,
            {"mode": result.mode, "returncode": result.returncode},
        )
        return {
            "task_id": task_id,
            "step_id": step["id"],
            "step": "TEST",
            "state": task_after["status"],
            "allowed": True,
            "reason": "LOCAL_TEST_PASSED",
            "result": payload,
        }


    def _verify_local_test(self, task_id):
        """Complete VERIFY only when persisted TEST evidence proves success."""
        import json

        steps = self.runner.load_steps(task_id)
        test_matches = [item for item in steps if item["name"] == "TEST"]
        verify_matches = [item for item in steps if item["name"] == "VERIFY"]

        if len(test_matches) != 1 or len(verify_matches) != 1:
            raise RuntimeError("VERIFY_STEP_STRUCTURE_INVALID")

        test_step = test_matches[0]
        verify_step = verify_matches[0]
        evidence = test_step.get("result")

        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except (ValueError, TypeError):
                evidence = None

        verified = (
            test_step.get("status") == "COMPLETED"
            and isinstance(evidence, dict)
            and evidence.get("passed") is True
            and evidence.get("returncode") == 0
            and evidence.get("timed_out") is False
            and (
                evidence.get("mode") == "compile"
                or (
                    isinstance(evidence.get("mode"), str)
                    and evidence["mode"].startswith("test:")
                    and evidence["mode"][5:] in ALLOWED_TESTS
                )
            )
        )

        if not verified:
            error = "VERIFY_FAILED: persisted TEST evidence is missing or unsuccessful"
            self.runner.state.fail_step_and_task(
                task_id, verify_step["id"], error,
                recovery_reason="TOOL_FAILURE",
            )
            self.runner.state.add_event(
                "workflow", "VERIFY_FAILED", task_id,
                {"test_step_id": test_step["id"]},
            )
            return {
                "task_id": task_id,
                "step_id": verify_step["id"],
                "step": "VERIFY",
                "state": "FAILED",
                "allowed": False,
                "reason": "VERIFY_FAILED",
            }

        result = {
            "verified": True,
            "test_step_id": test_step["id"],
            "test_mode": evidence["mode"],
            "test_returncode": evidence["returncode"],
        }
        task_after, _ = self.runner.state.complete_step_and_task(
            task_id, verify_step["id"], result=result
        )
        self.runner.state.add_event(
            "workflow", "VERIFY_COMPLETED", task_id, result
        )
        return {
            "task_id": task_id,
            "step_id": verify_step["id"],
            "step": "VERIFY",
            "state": task_after["status"],
            "allowed": True,
            "reason": "VERIFY_PASSED",
            "result": result,
        }


    def confirm_and_execute_step(
        self,
        task_id: str,
        step_name: str,
        capability: str,
        confirmation_token: str,
        arguments=None,
        operation=None,
        *,
        worker=None,
        reason=None,
    ):
        task = self.runner.load_task(task_id)
        if task["status"] != "BLOCKED_ON_USER_ACTION":
            return {
                "task_id": task_id,
                "step": step_name,
                "allowed": False,
                "reason": "CONFIRMATION_TASK_NOT_BLOCKED",
            }

        self.require_previous_step_completed(task_id, step_name)

        if step_name == "EDIT":
            self.require_verified_backup(task_id)

        steps = self.runner.load_steps(task_id)
        matches = [step for step in steps if step["name"] == step_name]
        if not matches:
            raise KeyError(f"Workflow step not found: {step_name}")

        step = matches[0]

        # The confirmation token is bound to the canonical arguments that
        # were stored when execute_step() created the challenge. Resolve
        # the persisted task root again before consuming the token so
        # relative project paths cannot produce a hash mismatch.
        resolved_arguments = TaskExecutor._resolve_target_root_arguments(
            task,
            capability,
            arguments,
        )

        ok, confirmation_reason = consume_confirmation(
            self.runner.state,
            task_id,
            capability,
            confirmation_token,
            operation=operation,
            arguments=resolved_arguments,
            step_id=step["id"],
        )

        if not ok:
            return {
                "task_id": task_id,
                "step_id": step["id"],
                "step": step_name,
                "allowed": False,
                "reason": confirmation_reason,
            }

        allowed, policy_reason = self.runner.authorize_action(
            task_id,
            step["id"],
            capability,
            operation,
            validated=False,
            confirmed=True,
        )

        if not allowed:
            return {
                "task_id": task_id,
                "step_id": step["id"],
                "step": step_name,
                "allowed": False,
                "reason": policy_reason,
            }

        executor = TaskExecutor(
            state=self.runner.state,
            worker=worker,
        )

        self.runner.transition(task_id, "RUNNING")

        return executor._run_step(
            task_id,
            step["id"],
            capability,
            resolved_arguments,
            operation,
            policy_reason,
            confirmed=True,
        )

    def verify_backup(self, task_id, target=None, backup_path=None):
        loaded = self.load(task_id)
        task, steps = loaded["task"], loaded["steps"]
        self.require_previous_step_completed(task_id, "VERIFY_BACKUP")
        verify_step = next((x for x in steps if x["name"] == "VERIFY_BACKUP"), None)
        backup_step = next((x for x in steps if x["name"] == "BACKUP"), None)
        if verify_step is None or backup_step is None:
            raise RuntimeError("WORKFLOW_BACKUP_STEPS_MISSING")

        backup_result = self._decode_step_result(backup_step)
        persisted_path = backup_result.get("backup_path") or backup_result.get("output")
        backup_target = backup_result.get("target") or "home"

        if backup_path is not None and (
            not persisted_path
            or TaskExecutor._normalize_backup_path(backup_path)
            != TaskExecutor._normalize_backup_path(persisted_path)
        ):
            return {"task_id": task_id, "state": task["status"],
                    "allowed": False, "reason": "BACKUP_PATH_MISMATCH"}
        if target is not None and str(target) != str(backup_target):
            return {"task_id": task_id, "state": task["status"],
                    "allowed": False, "reason": "BACKUP_TARGET_MISMATCH"}
        if verify_step["status"] == "COMPLETED":
            return {"task_id": task_id, "state": task["status"],
                    "allowed": True, "reason": "BACKUP_ALREADY_VERIFIED"}

        if task["status"] == "PLANNED":
            self.runner.transition(task_id, "RUNNING")
        elif task["status"] != "RUNNING":
            raise RuntimeError(f"BACKUP_VERIFY_INVALID_TASK_STATE:{task['status']}")

        if persisted_path:
            verified, reason = TaskExecutor._verify_backup_artifact(persisted_path)
        else:
            verified, reason = False, "BACKUP_PATH_MISSING"

        self.runner.state.add_test(
            "BACKUP_VERIFY", "PASSED" if verified else "FAILED",
            details={"path": persisted_path, "target": backup_target, "reason": reason},
            task_id=task_id,
        )
        if not verified:
            error = f"Backup artifact verification failed: {reason}"
            self.runner.state.fail_step_and_task(
                task_id, verify_step["id"], error,
                recovery_reason="BACKUP_VERIFY_FAILED",
            )
            self.runner.state.add_event(
                "workflow", "BACKUP_VERIFY_FAILED", task_id,
                {"step_id": verify_step["id"], "path": persisted_path,
                 "target": backup_target, "reason": reason},
            )
            return {"task_id": task_id, "state": "FAILED", "allowed": True,
                    "reason": "BACKUP_VERIFY_FAILED", "error": error}

        self.runner.state.add_backup(
            backup_target, persisted_path, "SUCCESS", task_id=task_id
        )
        retention = TaskExecutor(
            state=self.runner.state
        ).backup_retention.prune(backup_target)
        result = {
            "backup_path": persisted_path, "target": backup_target,
            "verified": True, "reason": reason, "retention": retention,
        }
        self.runner.state.complete_step_and_task(
            task_id, verify_step["id"], result=result
        )
        self.runner.state.add_event(
            "workflow", "BACKUP_VERIFIED", task_id,
            {"step_id": verify_step["id"], "path": persisted_path,
             "target": backup_target, "retention": retention},
        )
        return {"task_id": task_id, "state": "COMPLETED", "allowed": True,
                "reason": "BACKUP_VERIFIED", "result": result}

    def authorize_workflow_step(
        self,
        task_id: str,
        step_name: str,
        capability: str,
        operation=None,
        *,
        validated=False,
        confirmed=False,
    ):
        self.require_previous_step_completed(task_id, step_name)

        if step_name == "EDIT":
            self.require_verified_backup(task_id)

        return self.authorize_step(
            task_id,
            step_name,
            capability,
            operation,
            validated=validated,
            confirmed=confirmed,
        )

    def authorize_step(
        self,
        task_id: str,
        step_name: str,
        capability: str,
        operation=None,
        *,
        validated=False,
        confirmed=False,
    ):
        steps = self.runner.load_steps(task_id)
        matches = [s for s in steps if s["name"] == step_name]

        if not matches:
            raise KeyError(f"Workflow step not found: {step_name}")

        step = matches[0]

        allowed, reason = self.runner.authorize_action(
            task_id,
            step["id"],
            capability,
            operation,
            validated=validated,
            confirmed=confirmed,
        )

        return {
            "task_id": task_id,
            "step_id": step["id"],
            "step": step_name,
            "capability": capability,
            "operation": operation,
            "allowed": allowed,
            "reason": reason,
        }


    def require_previous_step_completed(self, task_id: str, step_name: str):
        steps = self.runner.load_steps(task_id)
        names = [step["name"] for step in steps]

        if step_name not in names:
            raise KeyError(f"Unknown workflow step: {step_name}")

        index = names.index(step_name)
        if index == 0:
            return True

        previous = steps[index - 1]
        if previous["status"] != "COMPLETED":
            raise RuntimeError(
                f"STEP_BLOCKED_PREVIOUS_NOT_COMPLETED:{previous['name']}"
            )

        return True

    def require_verified_backup(self, task_id: str):
        steps = self.runner.load_steps(task_id)
        by_name = {step["name"]: step for step in steps}
        backup, verify = by_name.get("BACKUP"), by_name.get("VERIFY_BACKUP")
        if backup is None or verify is None:
            raise RuntimeError("Stage 1 backup steps are missing")
        if backup["status"] != "COMPLETED":
            raise RuntimeError("EDIT_BLOCKED_BACKUP_NOT_COMPLETED")
        if verify["status"] != "COMPLETED":
            raise RuntimeError("EDIT_BLOCKED_BACKUP_NOT_VERIFIED")

        b = self._decode_step_result(backup)
        v = self._decode_step_result(verify)
        bp = b.get("backup_path") or b.get("output")
        vp = v.get("backup_path")
        bt, vt = b.get("target") or "home", v.get("target") or "home"
        if (
            not bp or not vp or v.get("verified") is not True
            or str(bt) != str(vt)
            or TaskExecutor._normalize_backup_path(bp)
               != TaskExecutor._normalize_backup_path(vp)
        ):
            raise RuntimeError("EDIT_BLOCKED_BACKUP_EVIDENCE_INVALID")
        return True
