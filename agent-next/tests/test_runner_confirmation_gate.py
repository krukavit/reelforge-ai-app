import os
import sys
import tempfile
from pathlib import Path


from app.runner import Runner
from app.state_store import StateStore


def test_blocked_task_rejects_unconfirmed_authorization():
    with tempfile.TemporaryDirectory() as td:
        runner = Runner(StateStore(Path(td) / "state.db"))
        runner.create_task("blocked-gate", profile="EDIT")
        runner.plan("blocked-gate")
        step_id = runner.add_step("blocked-gate", 1, "delete")
        allowed, reason = runner.authorize_action("blocked-gate", step_id, "delete")
        assert allowed is False and reason == "CONFIRM_REQUIRED"
        try:
            runner.authorize_action("blocked-gate", step_id, "delete", confirmed=False)
        except ValueError as exc:
            assert "consumed confirmation" in str(exc)
        else:
            raise AssertionError("blocked task accepted unconfirmed authorization")


def test_blocked_task_can_be_reauthorized_with_confirmation():
    with tempfile.TemporaryDirectory() as td:
        runner = Runner(StateStore(Path(td) / "state.db"))
        runner.create_task("blocked-confirm", profile="EDIT")
        runner.plan("blocked-confirm")
        step_id = runner.add_step("blocked-confirm", 1, "delete")
        allowed, reason = runner.authorize_action(
            "blocked-confirm", step_id, "delete", confirmed=True
        )
        assert allowed is True and reason == "CONFIRMED"


if __name__ == "__main__":
    test_blocked_task_rejects_unconfirmed_authorization()
    test_blocked_task_can_be_reauthorized_with_confirmation()
    print("RUNNER_CONFIRMATION_GATE_OK")
