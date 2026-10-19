from typing import Optional, Tuple

AUTO = {
    "system_info", "health", "process_list", "ls", "read", "search",
    "battery", "clipboard_get", "wifi_info", "public_ip", "weather",
    "speedtest", "diff", "cron_list", "diagnose", "optimize", "git_status",
    "recipe_list", "context", "session_list", "session_poll", "history",
    "cancel", "text_extract",
}

VALIDATED = {
    "backup", "qrcode", "image_process", "clipboard_set", "toast", "download",
    "open_url", "context_save", "session_start",
}

CONFIRM = {
    "process_kill", "write", "delete", "location", "sms_send", "sms_inbox",
    "restore", "cron_add", "cron_remove", "cloud_sync", "smart_install",
    "share", "git_pr", "recipe_run", "recipe_save", "session_kill",
    "history_save", "history_clear",
    "mkdir", "screenshot", "camera_photo", "notify", "tts_speak",
}

ALWAYS_BLOCK = {"run", "session_run"}

COMPOSITE_POLICIES = {
    "cloud_sync": {
        "list": "AUTO",
        "backup": "VALIDATED",
        "restore": "CONFIRM",
    },
    "image_process": {
        "info": "AUTO",
        "resize": "VALIDATED",
        "crop": "VALIDATED",
        "rotate": "VALIDATED",
    },
    "recipe_run": {
        "*": "CONFIRM",
    },
}


def authorize(
    capability: str,
    operation: Optional[str] = None,
    *,
    validated: bool = False,
    confirmed: bool = False,
) -> Tuple[bool, str]:
    """Authorize one MCP capability at the Policy boundary."""

    if capability in ALWAYS_BLOCK:
        return False, "ALWAYS_BLOCKED"

    composite = COMPOSITE_POLICIES.get(capability)
    if composite is not None:
        # Composite policies are authoritative per operation.
        # Never let the flat CONFIRM/VALIDATED sets override an
        # explicitly declared composite operation level.
        if operation is None:
            return False, "DENY_BY_DEFAULT"
        level = composite.get(operation, composite.get("*"))
        if level is None:
            return False, "DENY_BY_DEFAULT"
    elif capability in AUTO:
        level = "AUTO"
    elif capability in VALIDATED:
        level = "VALIDATED"
    elif capability in CONFIRM:
        level = "CONFIRM"
    else:
        return False, "DENY_BY_DEFAULT"

    if level == "AUTO":
        return True, "AUTO"
    if level == "VALIDATED":
        return (True, "VALIDATED") if validated else (False, "VALIDATION_REQUIRED")
    if level == "CONFIRM":
        return (True, "CONFIRMED") if confirmed else (False, "CONFIRM_REQUIRED")

    return False, "DENY_BY_DEFAULT"


def authorize_local_workflow_step(
    step_name: str,
    capability: str,
    operation: Optional[str],
) -> Tuple[bool, str]:
    """Authorize only the two fixed, non-MCP local workflow steps."""
    expected = {
        "TEST": ("local_test", {"compile", "test"}),
        "VERIFY": ("local_verify", {"persisted_result"}),
    }
    rule = expected.get(step_name)
    if rule is None:
        return False, "DENY_BY_DEFAULT"

    expected_capability, operations = rule
    if capability != expected_capability or operation not in operations:
        return False, "DENY_BY_DEFAULT"

    return True, "LOCAL_WORKFLOW_POLICY"
