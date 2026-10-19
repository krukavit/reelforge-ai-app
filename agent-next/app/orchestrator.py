from __future__ import annotations

from app.runner import PROFILES


STAGE1_STEPS = [
    "BACKUP",
    "VERIFY_BACKUP",
    "DIAGNOSE",
    "READ",
    "PLAN",
    "EDIT",
    "TEST",
    "VERIFY",
]


class AgentOrchestrator:
    """Deterministic planner/registrar above the existing workflow machinery."""

    def build_plan(
        self,
        task_description: str,
        profile: str = "EDIT",
        target_root=None,
    ) -> dict:
        if profile not in PROFILES:
            raise ValueError(f"invalid profile: {profile}")

        if not isinstance(task_description, str) or not task_description.strip():
            raise ValueError("task_description must be non-empty")

        master_task_id = self._make_id("master")
        workflows = []

        previous_task_id = None
        for index, name in enumerate(STAGE1_STEPS, start=1):
            task_id = self._make_id(f"stage1-{index}")
            dependencies = [] if previous_task_id is None else [previous_task_id]
            workflows.append({
                "task_id": task_id,
                "name": name,
                "profile": profile,
                "depends_on": dependencies,
            })
            previous_task_id = task_id

        return {
            "master_task_id": master_task_id,
            "task_description": task_description,
            "profile": profile,
            "target_root": target_root,
            "workflows": workflows,
            "execution_started": False,
            "mcp_calls": 0,
        }

    def create_from_plan(self, plan: dict) -> dict:
        from app.master_workflow import MasterWorkflowExecutor

        master = MasterWorkflowExecutor()
        result = master.create_from_plan_atomic(
            plan,
            target_root=plan.get("target_root"),
        )

        return {
            "master": result["master"],
            "children": result["children"],
            "execution_started": False,
            "mcp_calls": 0,
        }

    @staticmethod
    def _make_id(prefix: str) -> str:
        from uuid import uuid4
        return f"{prefix}-{uuid4().hex[:12]}"
