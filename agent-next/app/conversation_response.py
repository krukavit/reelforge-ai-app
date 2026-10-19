from __future__ import annotations

import json


def _system_info_summary(execution: dict) -> str:
    """Extract validated system metrics from the nested MCP result."""
    envelope = execution.get("result")
    if not isinstance(envelope, dict):
        return ""

    payload = envelope.get("result")
    if not isinstance(payload, dict):
        return ""

    content = payload.get("content")
    if not isinstance(content, list):
        return ""

    data = None
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        raw = item.get("text")
        if not isinstance(raw, str):
            continue
        try:
            candidate = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(candidate, dict):
            data = candidate
            break

    if not isinstance(data, dict):
        return ""

    def number(key):
        value = data.get(key)
        if isinstance(value, bool) or value is None:
            return None
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not result.is_integer() and key != "cpu_percent":
            return None
        return result

    parts = []

    ram_used = number("ram_mb_used")
    ram_total = number("ram_mb_total")
    if (
        ram_used is not None and ram_total is not None
        and 0 < ram_total <= 10_000_000
        and 0 <= ram_used <= ram_total
    ):
        parts.append(
            f"память занята {int(ram_used)} из {int(ram_total)} мегабайт"
        )

    disk_used = number("disk_mb_used")
    disk_total = number("disk_mb_total")
    if (
        disk_used is not None and disk_total is not None
        and 0 < disk_total <= 10_000_000_000
        and 0 <= disk_used <= disk_total
    ):
        parts.append(
            f"диск занят {int(disk_used)} из {int(disk_total)} мегабайт"
        )

    temperature = number("temp_celsius")
    if temperature is not None and -30 <= temperature <= 100:
        value = int(temperature) if temperature.is_integer() else temperature
        last_two = abs(int(temperature)) % 100 if temperature.is_integer() else 0
        last_one = abs(int(temperature)) % 10 if temperature.is_integer() else 0
        if temperature.is_integer() and last_two not in (11, 12, 13, 14):
            unit = "градус" if last_one == 1 else "градуса" if last_one in (2, 3, 4) else "градусов"
        else:
            unit = "градусов"
        parts.append(f"температура {value} {unit}")

    cpu = number("cpu_percent")
    if cpu is not None and 0 <= cpu <= 100:
        parts.append(f"загрузка процессора {cpu:g} процентов")

    uptime = number("uptime_seconds")
    if uptime is not None and 0 < uptime <= 10 * 365 * 24 * 3600:
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        if hours:
            parts.append(f"время работы {hours} часов {minutes} минут")
        elif minutes:
            parts.append(f"время работы {minutes} минут")
        else:
            parts.append(f"время работы {int(uptime)} секунд")

    if not parts:
        return ""
    return "Состояние системы: " + "; ".join(parts) + "."



def format_conversation_result(result: dict) -> str:
    """Turn one ConversationService result into a concise human-readable reply."""

    if not isinstance(result, dict):
        return "Не удалось обработать результат команды."

    error = result.get("error")
    if error:
        error_text = str(error).strip()

        if (
            "Не удалось однозначно определить файл для удаления." in error_text
            or "Не удалось однозначно определить файл для записи." in error_text
            or "Не удалось однозначно определить папку для создания." in error_text
        ):
            return error_text

        if "Не удалось однозначно" in error_text:
            return "Не удалось понять команду. Сформулируйте её точнее."

        if error_text.startswith("LLM intent rejected:"):
            return "Не удалось безопасно определить команду. Сформулируйте её точнее."

        return f"Не удалось выполнить команду: {error_text}"

    text = str(result.get("text") or "").strip()
    capability = result.get("capability")
    execution = result.get("result")

    if not isinstance(execution, dict):
        return "Не удалось получить результат выполнения команды."

    state = execution.get("state")
    reason = execution.get("reason")
    allowed = execution.get("allowed")

    if state == "BLOCKED_ON_USER_ACTION" and reason == "CONFIRM_REQUIRED":
        note = execution.get("confirmation_note")
        if isinstance(note, str) and note.strip():
            return f"Требуется подтверждение. {note.strip()}"
        return "Требуется подтверждение пользователя."

    if allowed is False:
        if reason == "NOT_AWAITING_CONFIRMATION":
            return "Не удалось выполнить команду: подтверждение уже недоступно."
        if reason:
            return f"Не удалось выполнить команду: {reason}."
        return "Не удалось выполнить команду."

    if state == "COMPLETED":
        if capability == "system_info":
            summary = _system_info_summary(execution)
            if summary:
                return summary
        if text:
            return f"Готово: {text}."
        return "Готово."

    if state == "FAILED":
        error = execution.get("error")
        if isinstance(error, str) and error.strip():
            return f"Не удалось выполнить команду: {error.strip()}"
        return "Не удалось выполнить команду."

    if state:
        return f"Команда завершилась со статусом: {state}."

    if capability:
        return f"Получен результат команды {capability}."

    return "Получен результат команды."


__all__ = ["format_conversation_result"]
