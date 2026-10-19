from __future__ import annotations

import json
from typing import Any

from ..conversation_service import ConversationService
from ..state_store import StateStore
from ..recovery import find_stuck_tasks


SERVER_NAME = "reelforge-agent-next"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-06-18"

EXPOSED_TOOLS = (
    "agent_chat",
    "agent_confirm",
    "agent_status",
)


class ExternalMCPError(Exception):
    pass


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "agent_chat",
            "description": (
                "Send a natural-language command to ReelForge Agent Next. "
                "The command passes through the existing parser, validator, "
                "policy authority, executor, and MCP worker."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Natural-language command for Agent Next.",
                    },
                    "target_root": {
                        "type": "string",
                        "description": (
                            "Optional isolated target root. "
                            "If omitted, Agent Next uses its normal project root."
                        ),
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
        {
            "name": "agent_confirm",
            "description": (
                "Resume one specific Agent Next task using its one-time "
                "confirmation token."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task waiting for confirmation.",
                    },
                    "confirmation_token": {
                        "type": "string",
                        "description": "One-time confirmation token.",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Optional replacement arguments.",
                    },
                },
                "required": ["task_id", "confirmation_token"],
                "additionalProperties": False,
            },
        },
        {
            "name": "agent_status",
            "description": "Read-only Agent Next task and worker status.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]


def _result(value: Any) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "structuredContent": value,
    }


def _error(message: str, code: str = "INVALID_REQUEST") -> dict[str, Any]:
    return {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": message,
            }
        ],
        "structuredContent": {
            "error": code,
            "message": message,
        },
    }


def handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "tools": {
                "listChanged": False,
            }
        },
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
        },
    }


def handle_tools_list() -> dict[str, Any]:
    return {
        "tools": _tool_definitions(),
    }


def handle_tools_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name not in EXPOSED_TOOLS:
        return _error(
            f"External tool is not exposed: {name}",
            "METHOD_NOT_FOUND",
        )

    if name == "agent_chat":
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error("'text' must be a non-empty string")

        target_root = arguments.get("target_root")
        if target_root is not None and not isinstance(target_root, str):
            return _error("'target_root' must be a string when supplied")

        result = ConversationService().handle(
            text,
            target_root=target_root,
        )
        return _result(result)

    if name == "agent_confirm":
        task_id = arguments.get("task_id")
        token = arguments.get("confirmation_token")

        if not isinstance(task_id, str) or not task_id:
            return _error("'task_id' must be a non-empty string")
        if not isinstance(token, str) or not token:
            return _error("'confirmation_token' must be a non-empty string")

        extra_arguments = arguments.get("arguments")
        if extra_arguments is not None and not isinstance(extra_arguments, dict):
            return _error("'arguments' must be an object when supplied")

        result = ConversationService().confirm(
            task_id,
            token,
            arguments=extra_arguments,
        )
        return _result(result)

    if name == "agent_status":
        store = StateStore()
        with store._connect() as con:
            tasks = [
                dict(row)
                for row in con.execute(
                    """
                    SELECT id, master_task, profile, status, stage,
                           created_at, finished_at, error, recovery_reason
                    FROM tasks
                    ORDER BY created_at DESC
                    LIMIT 20
                    """
                ).fetchall()
            ]
            workers = [
                dict(row)
                for row in con.execute(
                    """
                    SELECT id, task_id, status, pid,
                           started_at, finished_at, heartbeat_at
                    FROM workers
                    ORDER BY started_at DESC
                    LIMIT 20
                    """
                ).fetchall()
            ]

        return _result(
            {
                "status": "OK",
                "schema_version": store.get_schema_version(),
                "stuck_tasks": [task["id"] for task in find_stuck_tasks(store)],
                "tasks": tasks,
                "workers": workers,
            }
        )

    return _error("Unhandled external tool", "INTERNAL_ERROR")


__all__ = [
    "SERVER_NAME",
    "SERVER_VERSION",
    "PROTOCOL_VERSION",
    "EXPOSED_TOOLS",
    "handle_initialize",
    "handle_tools_list",
    "handle_tools_call",
]
