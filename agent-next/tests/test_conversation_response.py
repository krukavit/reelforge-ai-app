from app.conversation_response import format_conversation_result


def test_format_completed_system_info():
    result = {
        "text": "Проверь состояние агента",
        "capability": "system_info",
        "result": {
            "state": "COMPLETED",
            "allowed": True,
            "reason": "AUTO",
        },
    }

    text = format_conversation_result(result)

    assert "Готово" in text
    assert "состояние агента" in text
    assert "system_info" not in text


def test_format_confirmation_required():
    result = {
        "text": "Создай каталог test",
        "capability": "mkdir",
        "result": {
            "state": "BLOCKED_ON_USER_ACTION",
            "allowed": False,
            "reason": "CONFIRM_REQUIRED",
            "confirmation_token": "SECRET_TOKEN",
            "confirmation_note": "Одноразовый токен.",
        },
    }

    text = format_conversation_result(result)

    assert "Требуется подтверждение" in text
    assert "SECRET_TOKEN" not in text
    assert "Одноразовый токен" in text


def test_format_tool_failure():
    result = {
        "text": "Прочитай файл",
        "capability": "read",
        "result": {
            "state": "FAILED",
            "allowed": True,
            "reason": "TOOL_FAILURE",
            "error": "Cannot read: missing.txt",
        },
    }

    text = format_conversation_result(result)

    assert "Не удалось выполнить" in text
    assert "Cannot read: missing.txt" in text


def test_format_ambiguous_target_preserves_specific_guidance():
    result = {
        "text": "Удали какой-нибудь файл",
        "error": (
            "Не удалось однозначно определить файл для удаления. "
            "Укажите конкретный путь."
        ),
    }

    assert format_conversation_result(result) == (
        "Не удалось однозначно определить файл для удаления. "
        "Укажите конкретный путь."
    )


def test_format_ambiguous_mkdir_target_preserves_specific_guidance():
    result = {
        "text": "Создай какую-нибудь папку",
        "error": (
            "Не удалось однозначно определить папку для создания. "
            "Укажите конкретный путь."
        ),
    }

    assert format_conversation_result(result) == (
        "Не удалось однозначно определить папку для создания. "
        "Укажите конкретный путь."
    )


def test_format_parser_error():
    result = {
        "text": "Сделай что-нибудь",
        "error": "Не удалось однозначно определить команду",
    }

    text = format_conversation_result(result)

    assert "Не удалось понять команду" in text


def test_format_input_error():
    result = {
        "text": "Прочитай отсутствующий файл",
        "capability": "read",
        "error": "path does not exist",
    }

    text = format_conversation_result(result)

    assert "Не удалось выполнить" in text
    assert "path does not exist" in text

def test_format_llm_validation_error_hides_internal_details():
    result = {
        "text": "Сделай что-нибудь",
        "error": "LLM intent rejected: unknown capability: execute_anything",
    }

    text = format_conversation_result(result)

    assert text == "Не удалось безопасно определить команду. Сформулируйте её точнее."
    assert "LLM intent rejected" not in text
    assert "execute_anything" not in text


print("R5_LLM_RESPONSE_CONTRACT_TEST=GREEN")
print("PRODUCTION_MUTATION=NONE")


def test_format_system_info_speaks_valid_metrics_and_ignores_invalid_ones():
    import json

    stats = {
        "cpu_percent": "800",
        "ram_mb_total": 3413,
        "ram_mb_used": 2528,
        "disk_mb_total": 108335,
        "disk_mb_used": 73284,
        "temp_celsius": 43,
        "uptime_seconds": 0,
    }
    result = {
        "text": "Проверь состояние агента",
        "capability": "system_info",
        "result": {
            "state": "COMPLETED",
            "allowed": True,
            "reason": "AUTO",
            "result": {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps(stats),
                    }]
                },
            },
        },
    }

    text = format_conversation_result(result)

    assert text.startswith("Состояние системы:")
    assert "память занята 2528 из 3413 мегабайт" in text
    assert "диск занят 73284 из 108335 мегабайт" in text
    assert "температура 43 градуса" in text
    assert "800" not in text
    assert "загрузка процессора" not in text
    assert "время работы" not in text
    assert "system_info" not in text
    assert "{" not in text


def test_format_system_info_with_no_valid_metrics_falls_back_safely():
    import json

    result = {
        "text": "Проверь состояние агента",
        "capability": "system_info",
        "result": {
            "state": "COMPLETED",
            "allowed": True,
            "result": {
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "cpu_percent": "800",
                            "ram_mb_used": -1,
                            "ram_mb_total": 0,
                            "temp_celsius": 200,
                            "uptime_seconds": 0,
                        }),
                    }]
                }
            },
        },
    }

    text = format_conversation_result(result)
    assert text == "Готово: Проверь состояние агента."
