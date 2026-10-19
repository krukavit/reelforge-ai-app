from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tempfile
from pathlib import Path


from app.state_store import StateStore
from app.workflow import WorkflowExecutor


def make_stage2_env():
    td = tempfile.TemporaryDirectory()
    db = Path(td.name) / "stage2.db"
    store = StateStore(db)
    return td, store


def test_master_create_and_register_child_workflow():
    from app.master_workflow import MasterWorkflowExecutor

    td, store = make_stage2_env()
    try:
        master = MasterWorkflowExecutor(state=store)

        created = master.create_master("master-1")
        assert created["id"] == "master-1"
        assert created["master_task"] == "master-1"
        assert created["profile"] == "MASTER"
        assert created["status"] == "RECEIVED"

        child = master.add_stage1("master-1", "child-a", profile="EDIT")

        assert child["id"] == "child-a"
        assert child["master_task"] == "master-1"
        assert child["status"] == "PLANNED"

        current = master.status("master-1")
        assert current["master"]["id"] == "master-1"
        assert [item["id"] for item in current["children"]] == ["child-a"]
    finally:
        td.cleanup()


def test_master_requires_registered_child_completion():
    from app.master_workflow import MasterWorkflowExecutor

    td, store = make_stage2_env()
    try:
        master = MasterWorkflowExecutor(state=store)
        master.create_master("master-2")
        master.add_stage1("master-2", "child-a", profile="EDIT")

        result = master.reconcile("master-2")

        assert result["master"]["status"] != "COMPLETED"
        assert result["reason"] == "CHILD_WORKFLOW_INCOMPLETE"
    finally:
        td.cleanup()


def test_completed_child_allows_master_completion():
    from app.master_workflow import MasterWorkflowExecutor

    td, store = make_stage2_env()
    try:
        master = MasterWorkflowExecutor(state=store)
        master.create_master("master-3")
        master.add_stage1("master-3", "child-a", profile="EDIT")

        master.start_child("master-3", "child-a")
        child_steps = store.list_steps("child-a")
        assert len(child_steps) == 8

        for step in child_steps:
            store.complete_step_and_task(
                "child-a",
                step["id"],
                result={"test_fixture": "completed"},
            )

        result = master.reconcile("master-3")

        assert result["master"]["status"] == "COMPLETED"
    finally:
        td.cleanup()


def test_failed_child_prevents_master_completion():
    from app.master_workflow import MasterWorkflowExecutor

    td, store = make_stage2_env()
    try:
        master = MasterWorkflowExecutor(state=store)
        master.create_master("master-4")
        master.add_stage1("master-4", "child-a", profile="EDIT")

        master.start_child("master-4", "child-a")
        child_steps = store.list_steps("child-a")
        assert len(child_steps) == 8

        store.fail_step_and_task(
            "child-a",
            child_steps[0]["id"],
            error="child failure",
            recovery_reason="TEST_CHILD_FAILURE",
        )

        result = master.reconcile("master-4")

        assert result["master"]["status"] == "FAILED"
        assert result["reason"] == "CHILD_WORKFLOW_FAILED"
    finally:
        td.cleanup()


def test_duplicate_child_registration_is_rejected():
    from app.master_workflow import MasterWorkflowExecutor

    td, store = make_stage2_env()
    try:
        master = MasterWorkflowExecutor(state=store)
        master.create_master("master-5")
        master.add_stage1("master-5", "child-a", profile="EDIT")

        try:
            master.add_stage1("master-5", "child-a", profile="EDIT")
        except (ValueError, KeyError, RuntimeError):
            pass
        else:
            raise AssertionError("duplicate child registration was accepted")
    finally:
        td.cleanup()


def test_dependency_blocks_child_until_dependency_completes():
    from app.master_workflow import MasterWorkflowExecutor

    td, store = make_stage2_env()
    try:
        master = MasterWorkflowExecutor(state=store)
        master.create_master("master-6")
        master.add_stage1("master-6", "child-a", profile="EDIT")
        master.add_stage1("master-6", "child-b", profile="EDIT", depends_on=["child-a"])

        result = master.reconcile("master-6")

        assert result["master"]["status"] != "COMPLETED"
        assert result["reason"] in {
            "CHILD_WORKFLOW_INCOMPLETE",
            "DEPENDENCY_INCOMPLETE",
        }
    finally:
        td.cleanup()


def test_existing_stage1_workflow_remains_unchanged():
    td, store = make_stage2_env()
    try:
        wf = WorkflowExecutor(state=store)
        result = wf.create_stage1("stage1-compat", profile="EDIT")

        assert result["task_id"] == "stage1-compat"
        assert len(store.list_steps("stage1-compat")) == 8
    finally:
        td.cleanup()

def _complete_stage2_child(master, store, master_id, child_id):
    master.start_child(master_id, child_id)
    steps = store.list_steps(child_id)
    assert len(steps) == 8
    for step in steps:
        store.complete_step_and_task(
            child_id,
            step["id"],
            result={"test_fixture": "completed"},
        )


def _event_counts(store, task_id):
    with store._connect() as con:
        rows = con.execute(
            """
            SELECT event_type, COUNT(*) AS n
            FROM events
            WHERE task_id = ?
            GROUP BY event_type
            """,
            (task_id,),
        ).fetchall()
    return {row["event_type"]: row["n"] for row in rows}


def test_reconcile_completed_master_is_idempotent():
    from app.master_workflow import MasterWorkflowExecutor

    td, store = make_stage2_env()
    try:
        master = MasterWorkflowExecutor(state=store)
        master.create_master("master-idempotent")
        master.add_stage1("master-idempotent", "child-a", profile="EDIT")
        _complete_stage2_child(
            master, store, "master-idempotent", "child-a"
        )

        first = master.reconcile("master-idempotent")
        assert first["master"]["status"] == "COMPLETED"

        steps_before = store.list_steps("master-idempotent")
        events_before = _event_counts(store, "master-idempotent")
        task_before = store.get_task("master-idempotent")

        second = master.reconcile("master-idempotent")

        assert second["master"]["status"] == "COMPLETED"
        assert second["reason"] == "MASTER_TERMINAL"
        assert store.list_steps("master-idempotent") == steps_before
        assert _event_counts(store, "master-idempotent") == events_before

        task_after = store.get_task("master-idempotent")
        for key in ("status", "checkpoint", "started_at", "finished_at", "result"):
            assert task_after.get(key) == task_before.get(key), key
    finally:
        td.cleanup()


def test_reconcile_failed_master_is_immutable():
    from app.master_workflow import MasterWorkflowExecutor

    td, store = make_stage2_env()
    try:
        master = MasterWorkflowExecutor(state=store)
        master.create_master("master-failed-repeat")
        master.add_stage1(
            "master-failed-repeat", "child-a", profile="EDIT"
        )
        master.start_child("master-failed-repeat", "child-a")
        child_steps = store.list_steps("child-a")

        store.fail_step_and_task(
            "child-a",
            child_steps[0]["id"],
            error="fixture failure",
            recovery_reason="TEST_CHILD_FAILURE",
        )

        first = master.reconcile("master-failed-repeat")
        assert first["master"]["status"] == "FAILED"

        task_before = store.get_task("master-failed-repeat")
        steps_before = store.list_steps("master-failed-repeat")
        events_before = _event_counts(store, "master-failed-repeat")

        second = master.reconcile("master-failed-repeat")

        assert second["master"]["status"] == "FAILED"
        assert second["reason"] == "MASTER_TERMINAL"
        assert store.get_task("master-failed-repeat") == task_before
        assert store.list_steps("master-failed-repeat") == steps_before
        assert _event_counts(store, "master-failed-repeat") == events_before
    finally:
        td.cleanup()


def test_reconcile_rejects_corrupt_dependency_metadata():
    from app.master_workflow import MasterWorkflowExecutor

    td, store = make_stage2_env()
    try:
        master = MasterWorkflowExecutor(state=store)
        master.create_master("master-corrupt-metadata")
        master.add_stage1(
            "master-corrupt-metadata", "child-a", profile="EDIT"
        )
        _complete_stage2_child(
            master, store, "master-corrupt-metadata", "child-a"
        )

        # Corrupt only the temporary test database, not project files.
        with store._connect() as con:
            con.execute(
                "UPDATE tasks SET metadata = ? WHERE id = ?",
                ("{malformed-json", "child-a"),
            )

        result = master.reconcile("master-corrupt-metadata")

        assert result["reason"] == "CHILD_RECORD_CORRUPT"
        assert result["master"]["status"] != "COMPLETED"
        assert store.get_task("master-corrupt-metadata")["status"] != "COMPLETED"
    finally:
        td.cleanup()


def test_reconcile_rejects_invalid_dependency_graph():
    from app.master_workflow import MasterWorkflowExecutor

    td, store = make_stage2_env()
    try:
        master = MasterWorkflowExecutor(state=store)
        master.create_master("master-invalid-dependency")
        master.add_stage1(
            "master-invalid-dependency", "child-a", profile="EDIT"
        )
        _complete_stage2_child(
            master, store, "master-invalid-dependency", "child-a"
        )

        # A missing dependency must fail closed.
        with store._connect() as con:
            con.execute(
                "UPDATE tasks SET metadata = ? WHERE id = ?",
                ('{"dependencies":["missing-child"]}', "child-a"),
            )

        result = master.reconcile("master-invalid-dependency")

        assert result["reason"] == "DEPENDENCY_INVALID"
        assert result["master"]["status"] != "COMPLETED"
        assert store.get_task("master-invalid-dependency")["status"] != "COMPLETED"
    finally:
        td.cleanup()

if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("STAGE2_MASTER_WORKFLOW_CONTRACT_OK")
