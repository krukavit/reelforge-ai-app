

import os, sys, uuid
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from app.state_store import StateStore
from app.recovery import find_stuck_tasks, recover_stuck_tasks

DB_PATH = os.path.join(PROJECT_ROOT, "state", "test_recovery.db")
for suffix in ("", "-wal", "-shm"):
    try: os.remove(DB_PATH + suffix)
    except FileNotFoundError: pass
store = StateStore(DB_PATH)

stuck_id = "recovery-probe-" + uuid.uuid4().hex[:8]
store.create_task(stuck_id, master_task=stuck_id, profile="READ_ONLY", status="RECEIVED")
store.update_task(stuck_id, status="RUNNING", started_at="2026-09-12T00:00:00+00:00")

done_id = "recovery-control-" + uuid.uuid4().hex[:8]
store.create_task(done_id, master_task=done_id, profile="READ_ONLY", status="RECEIVED")
store.update_task(done_id, status="RUNNING", started_at="2026-09-12T00:00:00+00:00")
store.update_task(done_id, status="COMPLETED", finished_at="2026-09-12T00:00:05+00:00")

print("=== ДО recovery ===")
stuck_before = find_stuck_tasks(store)
print("найдено зависших:", [t["id"] for t in stuck_before])
assert stuck_id in [t["id"] for t in stuck_before]
assert done_id not in [t["id"] for t in stuck_before]

print("\n=== ЗАПУСК recovery (имитация restart процесса) ===")
recovered = recover_stuck_tasks(store, reason="PROCESS_RESTART")
print("восстановлено:", recovered)
assert stuck_id in recovered

print("\n=== ПОСЛЕ recovery ===")
stuck_task = store.get_task(stuck_id)
print("зависшая задача теперь:", dict(stuck_task))
assert stuck_task["status"] == "FAILED"
assert stuck_task["recovery_reason"] == "PROCESS_RESTART"
assert stuck_task["finished_at"] is not None

done_task = store.get_task(done_id)
assert done_task["status"] == "COMPLETED", "recovery не должен был трогать уже завершённую задачу"
print("контрольная задача не тронута:", done_task["status"])

print("\n=== ПОВТОРНЫЙ запуск recovery (не должно найти уже ничего) ===")
recovered_again = recover_stuck_tasks(store, reason="PROCESS_RESTART")
print("восстановлено во второй раз:", recovered_again)
assert recovered_again == [], "recovery не должен повторно трогать уже FAILED задачу"

print("\nВСЕ ПРОВЕРКИ RECOVERY ПРОШЛИ")
