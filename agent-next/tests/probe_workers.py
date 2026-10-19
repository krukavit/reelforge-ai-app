import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


from app.state_store import StateStore
from app.worker import Worker, find_abandoned_workers

DB_PATH = os.path.join(PROJECT_ROOT, "state", "test_workers.db")
for suffix in ("", "-wal", "-shm"):
    try: os.remove(DB_PATH + suffix)
    except FileNotFoundError: pass
store = StateStore(DB_PATH)

worker = Worker(
    store,
    task_id=None,
    metadata={"probe": True},
    worker_id="probe-worker-01",
)

assert store.get_worker("probe-worker-01") is not None

heartbeat = worker.heartbeat()
assert heartbeat["id"] == "probe-worker-01"

workers = store.list_workers(status="RUNNING")
assert any(w["id"] == "probe-worker-01" for w in workers)

worker.stop()
assert store.get_worker("probe-worker-01")["status"] == "STOPPED"
try:
    worker.heartbeat()
except RuntimeError as exc:
    assert "not RUNNING" in str(exc)
else:
    raise AssertionError("stopped worker accepted heartbeat")

print("WORKERS_PROBE_OK")
