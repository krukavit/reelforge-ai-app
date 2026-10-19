import tempfile
from pathlib import Path

from app.confirmation import create_confirmation, consume_confirmation
from app.executor import TaskExecutor
from app.state_store import StateStore


def test_executor_target_root_confirmation_roundtrip():
    with tempfile.TemporaryDirectory(prefix="confirmation-target-root-") as td:
        base = Path(td)
        db = base / "confirmation.db"
        target = base / "reelforge-project"
        target.mkdir()

        src = target / "src"
        src.mkdir()

        main_py = src / "main.py"
        main_py.write_text("print('ok')\n")

        other_py = src / "other.py"
        other_py.write_text("print('other')\n")

        state = StateStore(path=db)
        executor = TaskExecutor(state=state, worker=None)

        task_id = "confirmation-target-root-task"

        executor.runner.create_task(
            task_id,
            profile="EDIT",
            target_root=str(target),
        )

        task = state.get_task(task_id)

        canonical_args = executor._resolve_target_root_arguments(
            task,
            "write",
            {"path": "src/main.py"},
        )

        changed_args = executor._resolve_target_root_arguments(
            task,
            "write",
            {"path": "src/other.py"},
        )

        assert canonical_args["path"] == str(main_py.resolve())
        assert changed_args["path"] == str(other_py.resolve())
        assert canonical_args != changed_args

        token1 = create_confirmation(
            state,
            task_id=task_id,
            capability="write",
            operation="edit",
            step_no=1,
            arguments=canonical_args,
        )

        result1 = consume_confirmation(
            state,
            task_id=task_id,
            capability="write",
            token=token1,
            operation="edit",
            arguments=canonical_args,
        )

        assert result1 == (True, "CONFIRMED")

        replay = consume_confirmation(
            state,
            task_id=task_id,
            capability="write",
            token=token1,
            operation="edit",
            arguments=canonical_args,
        )

        assert replay[0] is False
        assert replay[1] == "ALREADY_CONSUMED"

        token2 = create_confirmation(
            state,
            task_id=task_id,
            capability="write",
            operation="edit",
            step_no=2,
            arguments=canonical_args,
        )

        changed_result = consume_confirmation(
            state,
            task_id=task_id,
            capability="write",
            token=token2,
            operation="edit",
            arguments=changed_args,
        )

        assert changed_result[0] is False

        original_result = consume_confirmation(
            state,
            task_id=task_id,
            capability="write",
            token=token2,
            operation="edit",
            arguments=canonical_args,
        )

        assert original_result == (True, "CONFIRMED")


if __name__ == "__main__":
    test_executor_target_root_confirmation_roundtrip()
    print("TARGET_ROOT_EXECUTOR_CONFIRMATION_TEST=GREEN")
