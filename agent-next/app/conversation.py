from __future__ import annotations

from dataclasses import dataclass, field
import re

from .capability_registry import CapabilityRegistry
from .policy import authorize


class ConversationAmbiguityError(ValueError):
    """Intent was recognized, but the target is too ambiguous to continue safely."""


@dataclass(frozen=True)
class ConversationIntent:
    text: str
    capability: str
    arguments: dict = field(default_factory=dict)
    profile: str = "READ_ONLY"
    operation: str | None = None
    allowed_by_policy: bool = False
    policy_reason: str = ""
    confidence: str = "high"


class RussianConversationParser:
    """
    Deterministic Russian-language adapter.

    This layer ONLY converts human text into an intent.
    It never supplies confirmed=True and never executes MCP calls.
    """

    def __init__(self, registry=None):
        self.registry = registry or CapabilityRegistry()
        self._known = set(self.registry.names())

    @staticmethod
    def _norm(text: str) -> str:
        text = str(text or "").strip().lower().replace("ё", "е")
        text = re.sub(r"\s+", " ", text)
        return text

    def parse(self, text: str) -> ConversationIntent:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be non-empty")

        raw = text.strip()
        t = self._norm(raw)

        capability, arguments, profile, operation = self._classify(t)

        if capability not in self._known:
            raise ValueError(f"unknown capability: {capability}")

        allowed, reason = authorize(capability, operation=operation)

        return ConversationIntent(
            text=raw,
            capability=capability,
            arguments=arguments,
            profile=profile,
            operation=operation,
            allowed_by_policy=allowed,
            policy_reason=reason,
        )

    def _classify(self, t: str):
        # --- destructive / modifying commands ---
        m = re.match(
            r"^(?:запиши|записать|сохрани|сохранить|запиши текст)\s+(.+?)\s+"
            r"(?:в|в файл|у файл)\s+(.+)$",
            t,
        )
        if m:
            content = m.group(1).strip()
            path = m.group(2).strip()
            if re.fullmatch(
                r"(?:какой(?:-нибудь|-либо)?|любой|любой(?:-нибудь)?)\s+файл",
                path,
            ):
                raise ConversationAmbiguityError("Не удалось однозначно определить файл для записи. Укажите конкретный путь.")
            return "write", {"path": path, "content": content}, "EDIT", None

        m = re.match(
            r"^(?:удали|удалить|стереть|сотри)\s+(?:файл\s+)?(.+)$",
            t,
        )
        if m:
            path = m.group(1).strip()
            if re.fullmatch(
                r"(?:какой(?:-нибудь|-либо)?|любой|любой(?:-нибудь)?)\s+файл",
                path,
            ):
                raise ConversationAmbiguityError("Не удалось однозначно определить файл для удаления. Укажите конкретный путь.")
            return "delete", {"path": path}, "EDIT", None

        if re.fullmatch(
            r"(?:создай|создать)\s+(?:папку|директорию|каталог)",
            t,
        ) or re.fullmatch(
            r"(?:создай|создать)\s+(?:какую(?:-нибудь|-либо)?|любую)\s+"
            r"(?:папку|директорию|каталог)",
            t,
        ):
            raise ConversationAmbiguityError(
                "Не удалось однозначно определить папку для создания. "
                "Укажите конкретный путь."
            )

        m = re.match(
            r"^(?:создай|создать)\s+(?:папку|директорию|каталог)\s+(.+)$",
            t,
        )
        if m:
            path = m.group(1).strip()
            if re.fullmatch(
                r"(?:какую(?:-нибудь|-либо)?|любую)\s+"
                r"(?:папку|директорию|каталог)",
                path,
            ):
                raise ConversationAmbiguityError(
                    "Не удалось однозначно определить папку для создания. "
                    "Укажите конкретный путь."
                )
            return "mkdir", {"path": path}, "EDIT", None

        # --- diagnostics / system ---
        if re.search(r"\b(?:полная диагностика|проведи диагностику|диагностика системы|проверь здоровье|проверь состояние агента)\b", t):
            return "health", {}, "READ_ONLY", None

        if re.search(r"\b(?:состояние системы|системная информация|ресурсы системы)\b", t):
            return "system_info", {}, "READ_ONLY", None

        if re.search(r"\b(?:диагностируй|диагностика)\b", t):
            intent = "all"
            if "python" in t:
                intent = "python"
            elif "git" in t:
                intent = "git"
            elif "хранилищ" in t or "storage" in t:
                intent = "storage"
            return "diagnose", {"intent": intent}, "READ_ONLY", None

        if re.search(r"\b(?:батаре[яю]|заряд|аккумулятор)\b", t):
            return "battery", {}, "READ_ONLY", None

        if re.search(r"\b(?:wifi|вайфай|вай-фай|в Wi.?Fi|вай фай)\b", t):
            return "wifi_info", {}, "READ_ONLY", None

        if re.search(r"\b(?:мой публичный ip|внешний ip|публичный ip|какой у меня ip)\b", t):
            return "public_ip", {}, "READ_ONLY", None

        if re.search(r"\b(?:оптимизируй|оптимизация|покажи производительность)\b", t):
            return "optimize", {}, "READ_ONLY", None

        if re.search(r"\b(?:скорость интернета|спидтест|speedtest|проверь скорость)\b", t):
            return "speedtest", {}, "READ_ONLY", None

        # --- conversational routing before generic "покажи ..." file listing ---
        if re.search(
            r"\b(?:покажи|покаж[ие]|выведи)\s+"
            r"(?:информацию\s+о\s+системе|системную\s+информацию)\b",
            t,
        ):
            return "system_info", {}, "READ_ONLY", None

        if re.search(
            r"\b(?:покажи|покаж[ие]|выведи)\s+"
            r"(?:процессы|список\s+процессов)\b",
            t,
        ):
            return "process_list", {}, "READ_ONLY", None

        # --- files ---
        m = re.match(
            r"^(?:покажи|покаж[ие]|выведи)\s+(?:файлы\s+)?(?:в\s+)?(.+)$",
            t,
        )
        if m:
            return "ls", {"path": m.group(1).strip()}, "READ_ONLY", None

        if re.search(r"\b(?:список файлов|что здесь|что в этой папке)\b", t):
            return "ls", {"path": "."}, "READ_ONLY", None

        m = re.match(
            r"^(?:прочитай|прочти|покажи содержимое)\s+(?:файл\s+)?(.+)$",
            t,
        )
        if m:
            return "read", {"path": m.group(1).strip()}, "READ_ONLY", None

        m = re.match(
            r"^открой\s+(?:файл\s+)?(.+)$",
            t,
        )
        if m:
            return "read", {"path": m.group(1).strip()}, "READ_ONLY", None

        m = re.match(
            r"^(?:найди|поищи)\s+(.+?)\s+(?:в папке|в)\s+(.+)$",
            t,
        )
        if m:
            return "search", {
                "pattern": m.group(1).strip(),
                "path": m.group(2).strip(),
            }, "READ_ONLY", None

        m = re.match(
            r"^(?:найди|поищи|поиск файлов?)\s+(.+)$",
            t,
        )
        if m:
            pattern = m.group(1).strip()
            pattern = re.sub(r"^строку\s+", "", pattern)
            return "search", {"pattern": pattern, "path": "."}, "READ_ONLY", None

        m = re.match(
            r"^(?:сравни|покажи разницу между|сделай diff между)\s+(.+?)\s+"
            r"(?:и|с)\s+(.+)$",
            t,
        )
        if m:
            return "diff", {
                "file": m.group(1).strip(),
                "file2": m.group(2).strip(),
            }, "READ_ONLY", None

        m = re.match(
            r"^(?:покажи git статус|git статус|статус git)(?:\s+(.+))?$",
            t,
        )
        if m:
            return "git_status", {
                "repo_dir": m.group(1).strip() if m.group(1) else "."
            }, "READ_ONLY", None

        # --- processes / sessions ---
        m = re.search(r"(?:первые|топ)\s+(\d+)\s+процесс", t)
        if m:
            return "process_list", {"limit": int(m.group(1))}, "READ_ONLY", None

        if re.search(r"\b(?:процессы|список процессов|что запущено)\b", t):
            return "process_list", {}, "READ_ONLY", None

        if re.search(r"\b(?:tmux сессии|tmux-сессии|сессии tmux|живые сессии)\b", t):
            return "session_list", {}, "READ_ONLY", None

        m = re.match(
            r"^(?:покажи вывод|опроси сессию|новый вывод)\s+(.+)$",
            t,
        )
        if m:
            return "session_poll", {"session": m.group(1).strip()}, "READ_ONLY", None

        # --- information / state ---
        if re.search(r"\b(?:покажи буфер|что в буфере обмена|буфер обмена)\b", t):
            return "clipboard_get", {}, "READ_ONLY", None

        if re.search(r"\b(?:сохраненный контекст|сохранённый контекст|покажи контекст)\b", t):
            return "context", {}, "READ_ONLY", None

        if re.search(r"\b(?:cron|крон|запланированные задачи|планировщик)\b", t):
            return "cron_list", {}, "READ_ONLY", None

        if re.search(r"\b(?:история задач|покажи историю|история агента)\b", t):
            return "history", {}, "READ_ONLY", None

        if re.search(r"\b(?:рецепты|сохраненные рецепты|сохранённые рецепты)\b", t):
            return "recipe_list", {}, "READ_ONLY", None

        # --- weather ---
        m = re.match(
            r"^(?:погода|какая погода|покажи погоду)(?:\s+(?:в|для)\s+(.+))?$",
            t,
        )
        if m and m.group(1):
            return "weather", {"city": m.group(1).strip()}, "READ_ONLY", None

        # --- OCR ---
        m = re.match(
            r"^(?:распознай текст|прочитай текст на картинке|сделай ocr)\s+(.+)$",
            t,
        )
        if m:
            return "text_extract", {"input": m.group(1).strip()}, "READ_ONLY", None

        raise ValueError("Не удалось однозначно определить команду из русской фразы")


__all__ = ["ConversationIntent", "RussianConversationParser"]
