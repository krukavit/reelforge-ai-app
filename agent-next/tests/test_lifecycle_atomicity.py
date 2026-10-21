from pathlib import Path
import tempfile

from app.runner import Runner
from app.state_store import StateStore


def fresh():
    return StateStore(Path(tempfile.mkdtemp()) / "state.db")


def test_multi_step_completion_does_not_complete_task_early():
    store = fresh()
    runner = Runner(store)
    runner.create_task("multi")
    runner.plan("multi")
    first = runner.add_step("multi", 1, "health")
    second = runner.add_step("multi", 2, "system_info")
    runner.transition("multi", "RUNNING")

    task, step = store.complete_step_and_task("multi", first, {"ok": 1})
    assert step["status"] == "COMPLETED"
    assert task["status"] == "RUNNING"
    assert store.get_step(second)["status"] != "COMPLETED"


def test_terminal_step_cannot_be_completed_or_failed_again():
    store = fresh()
    runner = Runner(store)
    runner.create_task("terminal")
    runner.plan("terminal")
    step = runner.add_step("terminal", 1, "health")
    runner.transition("terminal", "RUNNING")
    store.complete_step_and_task("terminal", step, {"ok": True})

    for fn in (
        lambda: store.complete_step_and_task("terminal", step, {"again": True}),
        lambda: store.fail_step_and_task("terminal", step, "late failure"),
    ):
        try:
            fn()
        except RuntimeError:
            pass
        else:
            raise AssertionError("terminal mutation was accepted")


if __name__ == "__main__":
    test_multi_step_completion_does_not_complete_task_early()
    test_terminal_step_cannot_be_completed_or_failed_again()
    print("LIFECYCLE_ATOMICITY_OK")
