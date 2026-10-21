from __future__ import annotations

import json
from app.state_store import StateStore
from app.runner import Runner, PROFILES


class MasterWorkflowExecutor:
    """Stage 2 master-task orchestration.

    This layer coordinates child workflows only. It does not execute MCP
    capabilities and does not replace WorkflowExecutor/Runner/Policy.
    """

    MASTER_PROFILE = "MASTER"

    def __init__(self, state=None):
        self.state = state or StateStore()

    def create_master(self, master_task_id, target_root=None):
        if not master_task_id:
            raise ValueError("master_task_id is required")
        if self.state.get_task(master_task_id):
            raise ValueError(f"Task already exists: {master_task_id}")

        return self.state.create_task(
            task_id=master_task_id,
            master_task=master_task_id,
            profile=self.MASTER_PROFILE,
            status="RECEIVED",
            target_root=target_root,
        )

    def add_stage1(self, master_task_id, child_task_id, profile="EDIT", depends_on=None):
        master = self.state.get_task(master_task_id)
        if master is None:
            raise KeyError(f"Master task not found: {master_task_id}")

        if self.state.get_task(child_task_id):
            raise ValueError(f"Child task already exists: {child_task_id}")

        if depends_on is None:
            dependencies = []
        else:
            dependencies = list(depends_on)

        if child_task_id in dependencies:
            raise ValueError("A child workflow cannot depend on itself")

        for dependency_id in dependencies:
            dependency = self.state.get_task(dependency_id)
            if dependency is None:
                raise KeyError(f"Dependency task not found: {dependency_id}")
            if dependency["master_task"] != master_task_id:
                raise ValueError(
                    f"Dependency belongs to another master task: {dependency_id}"
                )

        if profile not in PROFILES:
            raise ValueError(f"Unknown profile: {profile}")

        from app.workflow import STAGE1_STEPS

        self.state.create_stage1_child_atomic(
            task_id=child_task_id,
            master_task_id=master_task_id,
            profile=profile,
            dependencies=dependencies,
            step_names=STAGE1_STEPS,
            target_root=master["target_root"],
        )
        return self.state.get_task(child_task_id)

    def create_from_plan_atomic(self, plan, target_root=None):
        """Atomically register one master and its complete Stage1 plan."""
        if not isinstance(plan, dict):
            raise ValueError("plan must be a dict")

        master_task_id = plan.get("master_task_id")
        workflows = plan.get("workflows")

        if not master_task_id:
            raise ValueError("plan.master_task_id is required")
        if not isinstance(workflows, list) or not workflows:
            raise ValueError("plan.workflows must be non-empty")

        from app.workflow import STAGE1_STEPS

        seen = set()
        prepared = []

        for workflow in workflows:
            if not isinstance(workflow, dict):
                raise ValueError("workflow entries must be dicts")

            task_id = workflow.get("task_id")
            profile = workflow.get("profile")
            dependencies = list(workflow.get("depends_on", []))

            if not task_id:
                raise ValueError("workflow.task_id is required")
            if task_id in seen:
                raise ValueError(f"Duplicate child task: {task_id}")
            if task_id == master_task_id:
                raise ValueError("A child workflow cannot equal the master task")

            if profile not in PROFILES:
                raise ValueError(f"Unknown profile: {profile}")

            if task_id in dependencies:
                raise ValueError("A child workflow cannot depend on itself")

            for dependency_id in dependencies:
                if dependency_id not in seen:
                    raise ValueError(
                        f"Dependency must reference an earlier workflow: {dependency_id}"
                    )

            seen.add(task_id)
            prepared.append({
                "task_id": task_id,
                "profile": profile,
                "depends_on": dependencies,
                "step_names": list(STAGE1_STEPS),
            })

        master = self.state.create_master_with_stage1_plan_atomic(
            master_task_id=master_task_id,
            workflows=prepared,
            target_root=target_root,
        )

        return {
            "master": self.state.get_task(master_task_id),
            "children": [
                self.state.get_task(item["task_id"])
                for item in prepared
            ],
        }

    def start_child(self, master_task_id, child_task_id):
        """Start a registered child through the existing Runner lifecycle."""
        master = self.state.get_task(master_task_id)
        if master is None:
            raise KeyError(f"Master task not found: {master_task_id}")

        if master["status"] in {"FAILED", "COMPLETED"}:
            raise ValueError(
                f"Master task is terminal: {master_task_id}"
            )

        child = self.state.get_task(child_task_id)

        if child is None:
            raise KeyError(f"Child workflow not found: {child_task_id}")

        if child["master_task"] != master_task_id:
            raise ValueError(
                f"Child belongs to another master task: {child_task_id}"
            )

        if child["status"] == "RUNNING":
            raise ValueError(
                f"Child workflow already running: {child_task_id}"
            )

        if child["status"] == "BLOCKED_ON_USER_ACTION":
            raise ValueError(
                f"Child workflow requires confirmation: {child_task_id}"
            )

        if child["status"] == "FAILED":
            raise ValueError(
                f"Failed child requires recovery: {child_task_id}"
            )

        if child["status"] == "COMPLETED":
            raise ValueError(
                f"Child workflow already completed: {child_task_id}"
            )

        if not self.can_start_child(master_task_id, child_task_id):
            raise ValueError(
                f"Child workflow cannot start: {child_task_id}"
            )

        runner = Runner(state=self.state)
        return runner.transition(child_task_id, "RUNNING")

    def _read_child_dependencies(self, child):
        """Read dependency declarations without confusing them with task results."""
        raw_metadata = child.get("metadata")

        if raw_metadata is None or raw_metadata == "":
            # Compatibility is limited to an explicit legacy dependencies object.
            raw = child.get("result")
            if isinstance(raw, dict):
                payload = raw
            elif isinstance(raw, str) and raw.strip():
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    return None, "CHILD_RECORD_CORRUPT"
            else:
                return None, "CHILD_RECORD_CORRUPT"

            if not isinstance(payload, dict) or "dependencies" not in payload:
                return None, "CHILD_RECORD_CORRUPT"
        else:
            if isinstance(raw_metadata, dict):
                payload = raw_metadata
            elif isinstance(raw_metadata, str):
                try:
                    payload = json.loads(raw_metadata)
                except (TypeError, ValueError):
                    return None, "CHILD_RECORD_CORRUPT"
            else:
                return None, "CHILD_RECORD_CORRUPT"

            if not isinstance(payload, dict) or "dependencies" not in payload:
                return None, "CHILD_RECORD_CORRUPT"

        dependencies = payload["dependencies"]
        if (
            not isinstance(dependencies, list)
            or any(not isinstance(item, str) or not item for item in dependencies)
            or len(dependencies) != len(set(dependencies))
        ):
            return None, "CHILD_RECORD_CORRUPT"

        return dependencies, None

    def _completed_task_steps(self, task_id):
        """Require at least one step and require every step to be completed."""
        steps = self.state.list_steps(task_id)
        return bool(steps) and all(
            step.get("status") == "COMPLETED" for step in steps
        )

    def _dependency_check(self, master_task_id, child):
        dependencies, error = self._read_child_dependencies(child)
        if error:
            return error

        for dependency_id in dependencies:
            dependency = self.state.get_task(dependency_id)
            if (
                dependency is None
                or dependency.get("master_task") != master_task_id
                or dependency_id == child.get("id")
            ):
                return "DEPENDENCY_INVALID"

            if dependency.get("status") != "COMPLETED":
                return "DEPENDENCY_INCOMPLETE"

            if not self._completed_task_steps(dependency_id):
                return "DEPENDENCY_INCOMPLETE"

        return None

    def can_start_child(self, master_task_id, child_task_id):
        """Return whether a registered child may start safely."""
        master = self.state.get_task(master_task_id)
        if master is None:
            raise KeyError(f"Master task not found: {master_task_id}")

        if master.get("status") in {"FAILED", "COMPLETED"}:
            return False

        child = self.state.get_task(child_task_id)
        if child is None:
            raise KeyError(f"Child workflow not found: {child_task_id}")

        if child.get("master_task") != master_task_id:
            raise ValueError(
                f"Child belongs to another master task: {child_task_id}"
            )

        if child.get("status") in {
            "RUNNING", "BLOCKED_ON_USER_ACTION", "FAILED", "COMPLETED"
        }:
            return False

        return self._dependency_check(master_task_id, child) is None

    def reconcile(self, master_task_id):
        master = self.state.get_task(master_task_id)
        if master is None:
            raise KeyError(f"Master task not found: {master_task_id}")

        # Terminal master records are immutable under reconciliation.
        if master.get("status") in {"FAILED", "COMPLETED"}:
            result = self.status(master_task_id)
            result["reason"] = "MASTER_TERMINAL"
            return result

        current = self.status(master_task_id)
        children = current["children"]
        reason = None

        if not children:
            reason = "CHILD_WORKFLOW_INCOMPLETE"
        elif any(child.get("status") == "FAILED" for child in children):
            reason = "CHILD_WORKFLOW_FAILED"
        else:
            for child in children:
                if child.get("master_task") != master_task_id:
                    reason = "DEPENDENCY_INVALID"
                    break

                if child.get("status") != "COMPLETED":
                    reason = "CHILD_WORKFLOW_INCOMPLETE"
                    break

                if not self._completed_task_steps(child["id"]):
                    reason = "CHILD_WORKFLOW_INCOMPLETE"
                    break

                reason = self._dependency_check(master_task_id, child)
                if reason:
                    break

        if reason == "CHILD_WORKFLOW_FAILED":
            self.state.update_task(master_task_id, checkpoint=reason)
            # Let Runner validate the transition and set finished_at/event.
            Runner(state=self.state).transition(master_task_id, "FAILED")
        elif reason:
            # An incomplete graph is a checkpoint, not a master status change.
            self.state.update_task(master_task_id, checkpoint=reason)
        else:
            reason = "ALL_CHILD_WORKFLOWS_COMPLETED"
            steps = self.state.list_steps(master_task_id)
            reconcile_steps = [
                step for step in steps if step.get("name") == "RECONCILE"
            ]

            # A master workflow owns exactly one reconciliation step.
            if (
                len(reconcile_steps) > 1
                or any(step.get("name") != "RECONCILE" for step in steps)
            ):
                reason = "MASTER_RECORD_CORRUPT"
                self.state.update_task(master_task_id, checkpoint=reason)
            else:
                runner = Runner(state=self.state)
                master_status = master["status"]
                step = reconcile_steps[0] if reconcile_steps else None

                if master_status not in {
                    "RECEIVED", "PLANNED", "RUNNING", "WAITING"
                }:
                    reason = "MASTER_RECORD_CORRUPT"
                    self.state.update_task(master_task_id, checkpoint=reason)
                elif step and step.get("status") == "FAILED":
                    reason = "MASTER_RECORD_CORRUPT"
                    self.state.update_task(master_task_id, checkpoint=reason)
                else:
                    if master_status == "RECEIVED":
                        runner.transition(master_task_id, "PLANNED")
                        master_status = "PLANNED"

                    if master_status == "WAITING":
                        runner.transition(master_task_id, "RUNNING")
                        master_status = "RUNNING"

                    if step is None:
                        if master_status not in {"PLANNED", "RUNNING"}:
                            reason = "MASTER_RECORD_CORRUPT"
                            self.state.update_task(
                                master_task_id, checkpoint=reason
                            )
                        else:
                            step_id = runner.add_step(
                                master_task_id,
                                1,
                                "RECONCILE",
                            )
                            step = self.state.get_step(step_id)

                    if reason == "ALL_CHILD_WORKFLOWS_COMPLETED":
                        self.state.update_task(
                            master_task_id,
                            checkpoint="ALL_CHILD_WORKFLOWS_COMPLETED",
                        )

                        if master_status == "PLANNED":
                            runner.transition(master_task_id, "RUNNING")
                            master_status = "RUNNING"

                        if step and step.get("status") == "COMPLETED":
                            # Recover a consistent completed step and finalize atomically.
                            if not self._completed_task_steps(master_task_id):
                                reason = "MASTER_RECORD_CORRUPT"
                                self.state.update_task(
                                    master_task_id, checkpoint=reason
                                )
                            else:
                                self.state.complete_master_reconcile_atomic(
                                    master_task_id,
                                    step["id"],
                                    {"reason": "ALL_CHILD_WORKFLOWS_COMPLETED"},
                                )
                        elif step and master_status == "RUNNING":
                            self.state.complete_master_reconcile_atomic(
                                master_task_id,
                                step["id"],
                                {"reason": "ALL_CHILD_WORKFLOWS_COMPLETED"},
                            )
                        else:
                            reason = "MASTER_RECORD_CORRUPT"
                            self.state.update_task(
                                master_task_id, checkpoint=reason
                            )

        result = self.status(master_task_id)
        result["reason"] = reason
        return result

    def status(self, master_task_id):
        master = self.state.get_task(master_task_id)
        if master is None:
            raise KeyError(f"Master task not found: {master_task_id}")

        children = []
        with self.state._connect() as con:
            rows = con.execute(
                """
                SELECT *
                FROM tasks
                WHERE master_task = ? AND id != ?
                ORDER BY created_at, id
                """,
                (master_task_id, master_task_id),
            ).fetchall()
            children = [dict(row) for row in rows]

        return {
            "master": master,
            "children": children,
        }
