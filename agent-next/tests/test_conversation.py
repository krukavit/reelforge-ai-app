from app.conversation import RussianConversationParser


def test_health_russian():
    intent = RussianConversationParser().parse("Проверь состояние агента")
    assert intent.capability == "health"
    assert intent.arguments == {}
    assert intent.profile == "READ_ONLY"
    assert intent.allowed_by_policy is True


def test_read_russian():
    intent = RussianConversationParser().parse("Прочитай файл config/mcp.txt")
    assert intent.capability == "read"
    assert intent.arguments["path"] == "config/mcp.txt"
    assert intent.profile == "READ_ONLY"


def test_list_russian():
    intent = RussianConversationParser().parse("Покажи файлы в ~/reelforge-agent-next")
    assert intent.capability == "ls"
    assert intent.arguments["path"] == "~/reelforge-agent-next"


def test_weather_russian():
    intent = RussianConversationParser().parse("Какая погода в Москве")
    assert intent.capability == "weather"
    assert intent.arguments == {"city": "москве"}


def test_r10_system_info_phrase_wins_over_generic_ls():
    intent = RussianConversationParser().parse("Покажи информацию о системе")
    assert intent.capability == "system_info"
    assert intent.arguments == {}
    assert intent.profile == "READ_ONLY"
    assert intent.allowed_by_policy is True


def test_r10_process_list_phrase_wins_over_generic_ls():
    intent = RussianConversationParser().parse("Покажи процессы")
    assert intent.capability == "process_list"
    assert intent.arguments == {}
    assert intent.profile == "READ_ONLY"
    assert intent.allowed_by_policy is True


def test_r10_search_phrase_removes_russian_search_marker():
    intent = RussianConversationParser().parse("Найди строку hello")
    assert intent.capability == "search"
    assert intent.arguments == {"pattern": "hello", "path": "."}
    assert intent.profile == "READ_ONLY"
    assert intent.allowed_by_policy is True


def test_write_requires_existing_policy_confirmation():
    intent = RussianConversationParser().parse(
        "Запиши hello world в test.txt"
    )
    assert intent.capability == "write"
    assert intent.arguments == {
        "path": "test.txt",
        "content": "hello world",
    }
    assert intent.profile == "EDIT"
    assert intent.allowed_by_policy is False
    assert intent.policy_reason == "CONFIRM_REQUIRED"


def test_delete_requires_existing_policy_confirmation():
    intent = RussianConversationParser().parse("Удали файл test.txt")
    assert intent.capability == "delete"
    assert intent.arguments == {"path": "test.txt"}
    assert intent.profile == "EDIT"
    assert intent.allowed_by_policy is False
    assert intent.policy_reason == "CONFIRM_REQUIRED"


def test_write_ambiguous_target_is_rejected():
    parser = RussianConversationParser()
    try:
        parser.parse("Запиши hello в какой-нибудь файл")
    except ValueError as exc:
        assert "конкретный путь" in str(exc)
    else:
        raise AssertionError("ambiguous write target must be rejected")


def test_delete_ambiguous_target_is_rejected():
    parser = RussianConversationParser()
    try:
        parser.parse("Удали какой-нибудь файл")
    except ValueError as exc:
        assert "конкретный путь" in str(exc)
    else:
        raise AssertionError("ambiguous delete target must be rejected")


def test_mkdir_ambiguous_target_is_rejected():
    parser = RussianConversationParser()

    for command in (
        "Создай папку",
        "Создай какую-нибудь папку",
    ):
        try:
            parser.parse(command)
        except ValueError as exc:
            assert "конкретный путь" in str(exc)
        else:
            raise AssertionError("ambiguous mkdir target must be rejected")


def test_no_confirmed_argument_is_created():
    intent = RussianConversationParser().parse("Запиши hello в test.txt")
    assert "confirmed" not in intent.arguments


def test_mkdir_catalog_russian():
    intent = RussianConversationParser().parse("Создай каталог __chat_confirm_probe__")
    assert intent.capability == "mkdir"
    assert intent.arguments == {"path": "__chat_confirm_probe__"}
    assert intent.profile == "EDIT"
    assert intent.allowed_by_policy is False


def test_unknown_phrase_is_rejected():
    parser = RussianConversationParser()
    try:
        parser.parse("Сделай что-нибудь непонятное")
    except ValueError as exc:
        assert "Не удалось однозначно" in str(exc)
    else:
        raise AssertionError("unknown Russian phrase must be rejected")
