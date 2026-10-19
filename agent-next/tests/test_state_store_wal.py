import os
import uuid
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from app.state_store import StateStore


def test_state_store_uses_wal_and_persists():
    db = os.path.join(PROJECT_ROOT, "state", "test_wal.db")
    for suffix in ("", "-wal", "-shm"):
        try: os.remove(db + suffix)
        except FileNotFoundError: pass
    store = StateStore(db)
    task_id = "wal-" + uuid.uuid4().hex[:8]
    store.create_task(task_id, master_task=task_id, profile="READ_ONLY")
    check = StateStore(db).get_task(task_id)
    assert check and check["id"] == task_id
    with store._connect() as con:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal", mode
    for suffix in ("", "-wal", "-shm"):
        try: os.remove(db + suffix)
        except FileNotFoundError: pass


def test_task_step_numbers_are_unique():
    import tempfile
    from pathlib import Path
    from app.state_store import StateStore
    with tempfile.TemporaryDirectory() as d:
        store = StateStore(Path(d) / "state.db")
        tid = "unique-step-task"
        store.create_task(tid, tid, "READ_ONLY")
        store.add_step(tid, 1, "health")
        try:
            store.add_step(tid, 1, "health-again")
        except Exception as exc:
            assert "UNIQUE" in str(exc).upper()
        else:
            raise AssertionError("duplicate task step number accepted")


def test_complete_step_and_task_is_atomic_shape():
    import tempfile
    from pathlib import Path
    from app.state_store import StateStore
    with tempfile.TemporaryDirectory() as d:
        store = StateStore(Path(d) / "state.db")
        tid = "atomic-complete"
        store.create_task(tid, tid, "READ_ONLY")
        store.update_task(tid, status="RUNNING")
        sid = store.add_step(tid, 1, "health", status="RUNNING")
        task, step = store.complete_step_and_task(tid, sid, {"ok": True})
        assert task["status"] == "COMPLETED"
        assert step["status"] == "COMPLETED"
        assert task["finished_at"] and step["finished_at"]
