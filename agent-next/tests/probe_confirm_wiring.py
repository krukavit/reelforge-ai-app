import os, sys, uuid

from app.executor import TaskExecutor

ex = TaskExecutor()

print("=== 1. LLM/кто угодно пытается заявить confirmed=True напрямую в execute() ===")
try:
    ex.execute("forge-attempt-" + uuid.uuid4().hex[:8], "delete", confirmed=True)
    print("ПРОВАЛ: execute() принял confirmed как параметр!")
except TypeError as e:
    print(f"ЗАБЛОКИРОВАНО правильно: {e}")

print("\n=== 2. Честный путь: запрос delete без токена -> должен уйти в BLOCKED_ON_USER_ACTION ===")
task_id = "confirm-flow-" + uuid.uuid4().hex[:8]
first = ex.execute(task_id, "delete", arguments={"path": "/tmp/does-not-matter.txt"})
print(first)
assert first["state"] == "BLOCKED_ON_USER_ACTION"
assert "confirmation_token" in first
real_token = first["confirmation_token"]

print("\n=== 3. Попытка подделать confirm поддельным токеном на ТОТ ЖЕ task_id ===")
forged = ex.confirm_and_resume(task_id, "fake-llm-claims-user-said-yes")
print(forged)
assert forged["allowed"] is False and forged["reason"] == "NO_PENDING_CONFIRMATION"

print("\n=== 4. Попытка использовать настоящий токен, но на ЧУЖОЙ task_id ===")
other_task = "confirm-flow-" + uuid.uuid4().hex[:8]
try:
    stolen = ex.confirm_and_resume(other_task, real_token)
    print(stolen)
except KeyError as e:
    print(f"Ожидаемо: {e} (задача не существует)")

print("\n=== 5. Настоящий токен на СВОЙ task_id -> должно реально выполниться ===")
resumed = ex.confirm_and_resume(task_id, real_token, arguments={"path": "/tmp/does-not-matter.txt"})
print(resumed)

print("\n=== 6. Повторное использование ТОГО ЖЕ токена -> должно быть отклонено ===")
replay = ex.confirm_and_resume(task_id, real_token)
print(replay)
assert replay["allowed"] is False and replay["reason"] in ("ALREADY_CONSUMED", "NOT_AWAITING_CONFIRMATION")

print("\nВСЕ ПРОВЕРКИ test_llm_cannot_forge_confirm ПРОШЛИ")
