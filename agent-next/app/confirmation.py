import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from .state_store import StateStore, utc_now


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _hash_arguments(arguments):
    canonical = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_confirmation(store, task_id, capability, operation=None, step_no=None, ttl_seconds=300, arguments=None, step_id=None):
    """Создаёт одноразовый challenge, привязанный к конкретным task_id+capability.
    Возвращает "сырой" токен (единственный раз, когда он виден) - в БД хранится только его хэш."""
    token = secrets.token_urlsafe(16)
    confirmation_id = secrets.token_hex(8)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    with store._connect() as con:
        con.execute(
            """
            INSERT INTO confirmations
            (id, task_id, step_id, step_no, capability, operation, arguments_hash, token_hash, expires_at, consumed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (confirmation_id, task_id, step_id, step_no, capability, operation, _hash_arguments(arguments), _hash_token(token), expires_at),
        )
    return token


def consume_confirmation(store, task_id, capability, token, operation=None, arguments=None, step_id=None):
    """Единственный законный способ получить confirmed=True.
    Никогда не доверять голому confirmed=True, заявленному вызывающей стороной (LLM, CLI и т.д.)."""
    token_hash = _hash_token(token)
    with store._connect() as con:
        # Serialize token consumption so two concurrent callers cannot both
        # observe an unused token and execute the same confirmed action.
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT id, expires_at, consumed_at FROM confirmations
            WHERE task_id = ? AND step_id IS ? AND capability = ? AND operation IS ?
              AND arguments_hash = ? AND token_hash = ?
            ORDER BY id DESC LIMIT 1
            """,
            (task_id, step_id, capability, operation, _hash_arguments(arguments), token_hash),
        ).fetchone()

        if row is None:
            return False, "NO_PENDING_CONFIRMATION"
        if row["consumed_at"] is not None:
            return False, "ALREADY_CONSUMED"
        if row["expires_at"] < utc_now():
            return False, "EXPIRED"

        cur = con.execute(
            "UPDATE confirmations SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (utc_now(), row["id"]),
        )
        if cur.rowcount != 1:
            return False, "ALREADY_CONSUMED"
    return True, "CONFIRMED"
