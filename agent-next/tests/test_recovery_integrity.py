from pathlib import Path
import tempfile

from app.recovery import recover_stuck_tasks
from app.runner import Runner
from app.state_store import StateStore


def test_recovery_preserves_persisted_target_root():
    import tempfile
    from pathlib import Path

    from app.recovery import recover_stuck_tasks
    from app.state_store import StateStore

    with tempfile.TemporaryDirectory(prefix="recovery-target-root-") as tmp:
        base = Path(tmp)
        db = base / "state.db"
        target = base / "reelforge-project"
        target.mkdir()

        state = StateStore(path=db)
        task_id = "recovery-target-root-probe"

        state.create_task(
            task_id,
            master_task=task_id,
            profile="EDIT",
            target_root=str(target),
        )
        state.update_task(task_id, status="RUNNING", started_at="2026-09-23T02:00:00+00:00")

        before = state.get_task(task_id)
        expected_root = str(target.resolve())
        assert before["status"] == "RUNNING"
        assert before["target_root"] == expected_root

        recovered = recover_stuck_tasks(state, reason="PROCESS_RESTART")

        assert recovered == [task_id]

        after = state.get_task(task_id)
        assert after["status"] == "FAILED"
        assert after["recovery_reason"] == "PROCESS_RESTART"
        assert after["target_root"] == expected_root


def test_recovery_closes_unfinished_steps_without_touching_completed_steps():
    store = StateStore(Path(tempfile.mkdtemp()) / "state.db")
    runner = Runner(store)
    runner.create_task("recover")
    runner.plan("recover")
    first = runner.add_step("recover", 1, "health")
    second = runner.add_step("recover", 2, "system_info")
    runner.transition("recover", "RUNNING")
    store.update_step(first, status="COMPLETED", finished_at="2026-09-13T00:00:00+00:00")
    store.update_step(second, status="RUNNING", started_at="2026-09-13T00:01:00+00:00")

    assert recover_stuck_tasks(store) == ["recover"]
    assert store.get_task("recover")["status"] == "FAILED"
    steps = store.list_steps("recover")
    assert steps[0]["status"] == "COMPLETED"
    assert steps[1]["status"] == "FAILED"
    assert steps[1]["error"].startswith("Recovered on restart:")


if __name__ == "__main__":
    test_recovery_closes_unfinished_steps_without_touching_completed_steps()
    print("RECOVERY_INTEGRITY_OK")
