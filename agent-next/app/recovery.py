import os

from .state_store import StateStore


RECOVERY_REASONS = {
    "PROCESS_RESTART", "BRIDGE_RESTART", "CORE_RESTART", "NETWORK_FAILURE",
    "USER_ACTION", "TOOL_FAILURE", "POLICY_BLOCK", "BACKUP_VERIFY_FAILED", "UNKNOWN",
}


def mark_recovery(task, reason):
    if reason not in RECOVERY_REASONS:
        reason = "UNKNOWN"
    task["recovery_reason"] = reason
    return task


def find_stuck_tasks(store):
    """RUNNING-задачи, у которых нет finished_at - т.е. процесс умер
    посреди выполнения и никто их не закрыл. Не гадаем ПОЧЕМУ они
    зависли - это только обнаружение, не диагноз."""
    with store._connect() as con:
        rows = con.execute(
            "SELECT * FROM tasks WHERE status = 'RUNNING' AND finished_at IS NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def recover_stuck_tasks(store, reason="PROCESS_RESTART"):
    """Честно переводит зависшие RUNNING-задачи в FAILED с проставленным
    recovery_reason. Никогда не переводит их в COMPLETED и не делает вид,
    что они выполнились - только явный, видимый отказ, который потом
    можно перезапустить заново как новую задачу."""
    if reason not in RECOVERY_REASONS:
        reason = "UNKNOWN"

    recovered = []
    for task in find_stuck_tasks(store):
        task_id = task["id"]
        store.recover_task_and_steps(
            task_id,
            error="Recovered on restart: task was RUNNING with no finished_at",
            recovery_reason=reason,
        )
        store.add_event(
            "recovery", "TASK_RECOVERED", task_id,
            {"previous_status": "RUNNING", "reason": reason},
        )
        recovered.append(task_id)
    return recovered
