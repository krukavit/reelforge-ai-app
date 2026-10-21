"""Fail-closed sequential controller for the existing Stage 1 workflow."""

from __future__ import annotations

from app.workflow import STAGE1_STEPS, WorkflowExecutor
from app.local_test_runner import ALLOWED_TESTS


class WorkflowController:
    """Drive explicitly specified workflow steps without inventing tool inputs.

    This controller does not infer capabilities or arguments, bypass policy,
    or automatically resume steps requiring user confirmation.
    """

    def __init__(self, workflow=None):
        self.workflow = workflow or WorkflowExecutor()

    @staticmethod
    def _validate_specs(step_specs):
        if not isinstance(step_specs, dict):
            return "STEP_SPECS_MUST_BE_A_MAPPING"

        missing = [name for name in STAGE1_STEPS if name not in step_specs]
        if missing:
            return "MISSING_STEP_SPECS:" + ",".join(missing)

        unknown = sorted(set(step_specs) - set(STAGE1_STEPS))
        if unknown:
            return "UNKNOWN_STEP_SPECS:" + ",".join(unknown)

        for name in STAGE1_STEPS:
            spec = step_specs[name]
            if not isinstance(spec, dict):
                return f"INVALID_STEP_SPEC:{name}"
            if not isinstance(spec.get("capability"), str) or not spec["capability"].strip():
                return f"MISSING_CAPABILITY:{name}"
            if "arguments" not in spec:
                return f"MISSING_ARGUMENTS:{name}"
            if not isinstance(spec["arguments"], dict):
                return f"INVALID_ARGUMENTS:{name}"
            if name == "VERIFY_BACKUP" and spec.get("validated") is not True:
                return "VERIFY_BACKUP_REQUIRES_VALIDATED_TRUE"
            if name == "TEST":
                args = spec["arguments"]
                mode = args.get("mode")

                if mode not in {"compile", "test"}:
                    return "TEST_MODE_REQUIRED"

                if mode == "compile" and set(args) != {"mode"}:
                    return "TEST_ARGUMENTS_INVALID"

                if mode == "test":
                    if set(args) != {"mode", "test_name"}:
                        return "ALLOWLISTED_TEST_NAME_REQUIRED"
                    if not isinstance(args.get("test_name"), str) or args["test_name"] not in ALLOWED_TESTS:
                        return "TEST_NAME_NOT_ALLOWLISTED"

        return None

    def run(self, task_id, step_specs, *, worker=None):
        """Execute in order and stop at the first non-completed result.

        Each spec explicitly supplies capability, arguments and optional
        operation/validated/reason fields. Confirmation is never fabricated.
        """
        invalid = self._validate_specs(step_specs)
        if invalid:
            return {
                "task_id": task_id,
                "state": "NOT_STARTED",
                "completed_steps": [],
                "stopped_at": None,
                "reason": invalid,
            }

        completed = []

        for name in STAGE1_STEPS:
            try:
                current = self.workflow.load(task_id)
                task = current.get("task") or {}
                steps = current.get("steps") or []
                persisted = next((s for s in steps if s.get("name") == name), None)

                if persisted is None:
                    return self._stop(
                        task_id, completed, name, "PERSISTED_STEP_MISSING"
                    )

                if persisted.get("status") == "COMPLETED":
                    completed.append(name)
                    continue

                if task.get("status") in {
                    "FAILED", "COMPLETED", "BLOCKED_ON_USER_ACTION"
                }:
                    return self._stop(
                        task_id,
                        completed,
                        name,
                        "TASK_NOT_RUNNABLE:" + str(task.get("status")),
                    )

                spec = step_specs[name]
                result = self.workflow.execute_step(
                    task_id,
                    name,
                    spec["capability"],
                    arguments=spec["arguments"],
                    operation=spec.get("operation"),
                    validated=spec.get("validated", False),
                    reason=spec.get("reason") or "WORKFLOW_CONTROLLER_B1",
                    worker=worker,
                )

                # Read durable state after each step; never infer completion
                # merely from a truthy response or an "allowed" field.
                after = self.workflow.load(task_id)
                after_task = after.get("task") or {}
                after_steps = after.get("steps") or []
                saved = next((s for s in after_steps if s.get("name") == name), None)

                if saved is not None and saved.get("status") == "COMPLETED":
                    completed.append(name)
                    continue

                reason = None
                if isinstance(result, dict):
                    reason = result.get("reason")
                    if result.get("confirmation_token"):
                        reason = reason or "CONFIRMATION_REQUIRED"
                if not reason:
                    reason = (
                        "TASK_FAILED"
                        if after_task.get("status") == "FAILED"
                        else "STEP_NOT_PERSISTED_AS_COMPLETED"
                    )

                safe_result = self._redact_confirmation_tokens(result)
                return self._stop(
                    task_id, completed, name, str(reason), result=safe_result,
                    task_state=after_task.get("status"),
                )

            except Exception as exc:
                # Report the exception type/message for diagnosis, but do not
                # retry: the underlying operation may have an unknown outcome.
                return self._stop(
                    task_id,
                    completed,
                    name,
                    "EXECUTION_EXCEPTION",
                    error_type=type(exc).__name__,
                )

        try:
            final = self.workflow.load(task_id)
        except Exception as exc:
            # All steps appeared complete, but the final durable-state
            # check failed. Never report success without that verification.
            return self._stop(
                task_id,
                completed,
                "FINAL_STATE_CHECK",
                "FINAL_STATE_READ_FAILED",
                error_type=type(exc).__name__,
            )

        final_task = final.get("task") or {}
        final_steps = final.get("steps")

        # Fail closed: persisted stage records must be well-formed,
        # unique, and contain exactly the required Stage 1 step names.
        if not isinstance(final_steps, list):
            return self._stop(
                task_id,
                completed,
                "FINAL_STATE_CHECK",
                "FINAL_STATE_INVALID_STEP_RECORDS",
            )

        seen_names = set()
        invalid_records = False
        for record in final_steps:
            if not isinstance(record, dict):
                invalid_records = True
                break
            name = record.get("name")
            if not isinstance(name, str) or name not in STAGE1_STEPS:
                invalid_records = True
                break
            if name in seen_names:
                invalid_records = True
                break
            seen_names.add(name)

        if invalid_records or seen_names != set(STAGE1_STEPS):
            return self._stop(
                task_id,
                completed,
                "FINAL_STATE_CHECK",
                "FINAL_STATE_INVALID_STEP_RECORDS",
            )

        all_steps_complete = all(
            record.get("status") == "COMPLETED"
            for record in final_steps
        )
        task_complete = (
            isinstance(final_task, dict)
            and final_task.get("status") == "COMPLETED"
        )
        all_complete = task_complete and all_steps_complete

        if all_complete:
            final_reason = "ALL_STEPS_AND_TASK_PERSISTED_COMPLETED"
        elif not task_complete:
            final_reason = "FINAL_TASK_STATE_NOT_COMPLETED"
        else:
            final_reason = "FINAL_STATE_NOT_COMPLETE"

        return {
            "task_id": task_id,
            "state": "COMPLETED" if all_complete else "INCOMPLETE",
            "task_state": final_task.get("status"),
            "completed_steps": completed,
            "stopped_at": None if all_complete else "FINAL_STATE_CHECK",
            "reason": final_reason,
        }

    @staticmethod
    def _redact_confirmation_tokens(value):
        """Remove confirmation secrets from nested diagnostic results."""
        if isinstance(value, dict):
            return {
                key: WorkflowController._redact_confirmation_tokens(item)
                for key, item in value.items()
                if not (
                    isinstance(key, str)
                    and key.lower() == "confirmation_token"
                )
            }
        if isinstance(value, list):
            return [
                WorkflowController._redact_confirmation_tokens(item)
                for item in value
            ]
        if isinstance(value, tuple):
            return tuple(
                WorkflowController._redact_confirmation_tokens(item)
                for item in value
            )
        return value

    @staticmethod
    def _stop(task_id, completed, step, reason, **extra):
        return {
            "task_id": task_id,
            "state": "STOPPED",
            "completed_steps": list(completed),
            "stopped_at": step,
            "reason": reason,
            **extra,
        }
