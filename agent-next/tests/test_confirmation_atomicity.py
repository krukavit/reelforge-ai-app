import os
import sys

import tempfile
import threading
from pathlib import Path

from app.confirmation import create_confirmation, consume_confirmation
from app.state_store import StateStore


def test_confirmation_is_one_time_under_concurrent_consumers():
    with tempfile.TemporaryDirectory() as td:
        store = StateStore(Path(td) / "state.db")
        task_id = "atomic-confirmation-task"
        store.create_task(task_id, task_id, "EDIT")
        token = create_confirmation(
            store, task_id, "delete", arguments={"path": "/x"}
        )

        results = []
        lock = threading.Lock()

        def consume():
            result = consume_confirmation(
                store, task_id, "delete", token, arguments={"path": "/x"}
            )
            with lock:
                results.append(result)

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(ok for ok, _ in results) == 1, results
        assert len(results) == 2
        assert sorted(reason for _, reason in results) == [
            "ALREADY_CONSUMED", "CONFIRMED"
        ]


def test_confirmation_is_bound_to_exact_step_id():
    with tempfile.TemporaryDirectory() as td:
        store = StateStore(Path(td) / "state.db")
        task_id = "step-binding-task"
        store.create_task(task_id, task_id, "EDIT")
        token = create_confirmation(
            store, task_id, "delete", arguments={"path": "/x"}, step_id=10
        )
        ok, reason = consume_confirmation(
            store, task_id, "delete", token, arguments={"path": "/x"}, step_id=11
        )
        assert (ok, reason) == (False, "NO_PENDING_CONFIRMATION")
        ok, reason = consume_confirmation(
            store, task_id, "delete", token, arguments={"path": "/x"}, step_id=10
        )
        assert (ok, reason) == (True, "CONFIRMED")


if __name__ == "__main__":
    test_confirmation_is_one_time_under_concurrent_consumers()
    test_confirmation_is_bound_to_exact_step_id()
    print("CONFIRMATION_ATOMICITY_OK")
