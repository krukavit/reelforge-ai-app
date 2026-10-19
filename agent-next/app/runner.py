from datetime import datetime, timezone

from .policy import authorize
from .state_store import StateStore


PROFILES = {
    "READ_ONLY",
    "CREATE",
    "EDIT",
    "DEPLOY",
    "RECOVERY",
    "MAINTENANCE",
}

STATES = {
    "RECEIVED",
    "PLANNED",
    "RUNNING",
    "WAITING",
    "COMPLETED",
    "FAILED",
    "BLOCKED_ON_USER_ACTION",
}

TRANSITIONS = {
    "RECEIVED": {"PLANNED", "FAILED"},
    "PLANNED": {"RUNNING", "BLOCKED_ON_USER_ACTION", "FAILED"},
    "RUNNING": {
        "WAITING",
        "COMPLETED",
        "FAILED",
        "BLOCKED_ON_USER_ACTION",
    },
    "WAITING": {"RUNNING", "FAILED", "BLOCKED_ON_USER_ACTION"},
    "BLOCKED_ON_USER_ACTION": {"RUNNING", "FAILED"},
    "FAILED": {"PLANNED", "RUNNING"},
    "COMPLETED": set(),
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Runner:
    def __init__(self, state=None):
        self.state = state or StateStore()

    def create_task(
        self,
        task_id,
        master_task=None,
        profile="READ_ONLY",
        target_root=None,
    ):
        if profile not in PROFILES:
            raise ValueError(f"Unknown profile: {profile}")

        if self.state.get_task(task_id):
            raise ValueError(f"Task already exists: {task_id}")

        task = self.state.create_task(
            task_id=task_id,
            master_task=master_task or task_id,
            profile=profile,
            status="RECEIVED",
            target_root=target_root,
        )

        self.state.add_event(
            "runner",
            "TASK_CREATED",
            task_id,
            {"profile": profile},
        )
        return task

    def transition(self, task_id, new_state):
        if new_state not in STATES:
            raise ValueError(f"Unknown state: {new_state}")

        task = self.state.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")

        old_state = task["status"]

        if new_state != old_state and new_state not in TRANSITIONS[old_state]:
            raise ValueError(
                f"Invalid transition: {old_state} -> {new_state}"
            )

        fields = {"status": new_state}

        if new_state == "RUNNING" and not task["started_at"]:
            fields["started_at"] = utc_now()

        if new_state == "COMPLETED":
            steps = self.state.list_steps(task_id)
            if not steps:
                raise ValueError("Task cannot be COMPLETED without steps")
            incomplete = [
                step["id"] for step in steps if step["status"] != "COMPLETED"
            ]
            if incomplete:
                raise ValueError(
                    f"Task cannot be COMPLETED while steps are incomplete: {incomplete}"
                )
            fields["finished_at"] = utc_now()

        if new_state == "FAILED":
            fields["finished_at"] = utc_now()

        updated = self.state.update_task(task_id, **fields)

        self.state.add_event(
            "runner",
            "STATE_CHANGED",
            task_id,
            {"from": old_state, "to": new_state},
        )
        return updated

    def plan(self, task_id, stage=None, checkpoint=None):
        task = self.state.get_task(task_id)

        if not task:
            raise KeyError(f"Task not found: {task_id}")

        if task["status"] != "RECEIVED":
            raise ValueError(
                f"Task cannot be planned from {task['status']}"
            )

        fields = {}
        if stage is not None:
            fields["stage"] = stage
        if checkpoint is not None:
            fields["checkpoint"] = checkpoint

        updated = self.state.update_task(task_id, **fields)
        return self.transition(task_id, "PLANNED")

    def add_step(self, task_id, step_no, name):
        task = self.state.get_task(task_id)

        if not task:
            raise KeyError(f"Task not found: {task_id}")
        if task["status"] not in {"PLANNED", "RUNNING", "WAITING"}:
            raise ValueError(
                f"Step cannot be added while task is {task['status']}"
            )

        return self.state.add_step(
            task_id,
            step_no,
            name,
            status="RECEIVED",
        )

    def authorize_action(
        self,
        task_id,
        step_id,
        capability,
        operation=None,
        *,
        validated=False,
        confirmed=False,
        arguments=None,
    ):
        task = self.state.get_task(task_id)

        if not task:
            raise KeyError(f"Task not found: {task_id}")

        step = self.state.get_step(step_id)
        if not step:
            raise KeyError(f"Step not found: {step_id}")
        if step["task_id"] != task_id:
            raise ValueError(f"Step {step_id} does not belong to task {task_id}")
        if task["status"] not in {"PLANNED", "RUNNING", "WAITING", "BLOCKED_ON_USER_ACTION"}:
            raise ValueError(
                f"Action cannot be authorized while task is {task['status']}"
            )
        if task["status"] == "BLOCKED_ON_USER_ACTION" and not confirmed:
            raise ValueError(
                "Blocked task can only be re-authorized with a consumed confirmation"
            )

        allowed, reason = authorize(
            capability,
            operation,
            validated=validated,
            confirmed=confirmed,
        )

        authorization = {
            "capability": capability,
            "operation": operation,
            "allowed": allowed,
            "reason": reason,
            "validated": validated,
            "confirmed": confirmed,
        }
        if arguments is not None:
            authorization["arguments"] = arguments

        self.state.update_step(
            step_id,
            authorization=authorization,
        )

        if not allowed and reason == "CONFIRM_REQUIRED":
            self.transition(task_id, "BLOCKED_ON_USER_ACTION")

        return allowed, reason

    def checkpoint(self, task_id, checkpoint, stage=None):
        fields = {"checkpoint": checkpoint}

        if stage is not None:
            fields["stage"] = stage

        updated = self.state.update_task(task_id, **fields)

        self.state.add_event(
            "runner",
            "CHECKPOINT",
            task_id,
            {"checkpoint": checkpoint, "stage": stage},
        )
        return updated

    def load_task(self, task_id):
        return self.state.get_task(task_id)

    def load_steps(self, task_id):
        return self.state.list_steps(task_id)
