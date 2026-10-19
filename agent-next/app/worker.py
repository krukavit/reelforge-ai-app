import os
import uuid


from .state_store import utc_now


class Worker:
    """Минимальная обёртка: один worker привязан к одной задаче,
    обязан регулярно слать heartbeat, иначе будет считаться заброшенным."""

    def __init__(self, store, task_id=None, metadata=None, worker_id=None):
        self.store = store
        self.id = worker_id or ("worker-" + uuid.uuid4().hex[:12])
        self.task_id = task_id
        self.store.add_worker(
            self.id, task_id=task_id, status="RUNNING",
            pid=os.getpid(), metadata=metadata,
        )

    def heartbeat(self):
        return self.store.heartbeat_worker(self.id)

    def stop(self, status="STOPPED"):
        return self.store.update_worker(
            self.id, status=status, finished_at=utc_now(),
        )


def find_abandoned_workers(store, stale_after_seconds=60):
    """RUNNING-воркеры, чей последний heartbeat старше порога.
    Не решает, ЧТО с ними делать - только обнаруживает, отдельно
    от recovery.py по задачам (это про воркеры, не про tasks)."""
    from datetime import datetime, timedelta, timezone

    threshold = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    abandoned = []
    for w in store.list_workers(status="RUNNING"):
        hb = w.get("heartbeat_at")
        if not hb:
            abandoned.append(w)
            continue
        hb_time = datetime.fromisoformat(hb)
        if hb_time < threshold:
            abandoned.append(w)
    return abandoned
