"""Strict Local Agent protocol and fail-closed desktop command validation."""

import json
from typing import Any, Dict


MAX_MESSAGE_BYTES = 8192
ALLOWED_ACTIONS = {
    "open_app": {"app_id": {"calculator", "browser", "text_editor"}},
    "capture_screen": {},
    "get_active_window": {},
    "input_text": {"text": {"max_chars": 500}},
    "read_file": {"path": {"max_chars": 1024}},
    "write_file": {"path": {"max_chars": 1024}, "content": {"max_chars": 65536}},
}


def parse_message(message: Any) -> Dict[str, Any]:
    if not isinstance(message, str):
        raise ValueError("Message is invalid or too large")
    try:
        if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("Message is invalid or too large")
    except UnicodeEncodeError as exc:
        raise ValueError("Message is invalid or too large") from exc

    def reject_duplicate_keys(pairs):
        parsed = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError("Duplicate JSON field")
            parsed[key] = value
        return parsed

    def reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    try:
        parsed = json.loads(
            message,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Message is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Message must be a JSON object")
    return parsed


def validate_command(command: Any) -> Dict[str, Any]:
    if not isinstance(command, dict):
        raise ValueError("Command must be an object")

    if set(command) != {
        "type",
        "command_id",
        "action",
        "payload",
        "execution_enabled",
    }:
        raise ValueError("Command fields are invalid")
    if command["type"] != "command":
        raise ValueError("Message type is invalid")
    if (
        not isinstance(command["command_id"], str)
        or not command["command_id"]
        or len(command["command_id"]) > 64
    ):
        raise ValueError("Command ID is invalid")
    if not isinstance(command["execution_enabled"], bool):
        raise ValueError("Execution flag is invalid")

    action = command["action"]
    payload = command["payload"]
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS or not isinstance(payload, dict):
        raise ValueError("Action or payload is invalid")

    if action == "open_app":
        if (
            set(payload) != {"app_id"}
            or not isinstance(payload.get("app_id"), str)
            or payload["app_id"] not in ALLOWED_ACTIONS[action]["app_id"]
        ):
            raise ValueError("Application is not allowlisted")
    elif action == "capture_screen" or action == "get_active_window":
        if payload:
            raise ValueError("Action does not accept a payload")
    elif action == "input_text":
        if set(payload) != {"text"} or not isinstance(payload["text"], str):
            raise ValueError("Input text payload is invalid")
        if not 1 <= len(payload["text"]) <= 500:
            raise ValueError("Input text is outside the safety limit")
    elif action in {"read_file", "write_file"}:
        if action == "read_file":
            if set(payload) != {"path"}:
                raise ValueError("Restricted file payload is invalid")
        elif set(payload) != {"path", "content"} or not isinstance(payload["content"], str):
            raise ValueError("Restricted file payload is invalid")
        if not isinstance(payload["path"], str) or not payload["path"] or len(payload["path"]) > 1024:
            raise ValueError("Restricted file path is invalid")
        if action == "write_file" and len(payload["content"].encode("utf-8")) > 65536:
            raise ValueError("Restricted file content is too large")
    else:
        raise ValueError("Action does not accept a payload")

    return command


def handle_command(command: Any, runtime=None) -> Dict[str, Any]:
    """Validate and execute a command only through the opt-in desktop runtime."""
    validated = validate_command(command)
    if validated["execution_enabled"] is not True:
        return {
            "command_id": validated["command_id"],
            "action": validated["action"],
            "success": False,
            "message": "Desktop execution is disabled",
        }

    if runtime is None:
        from desktop_runtime import DesktopRuntime
        runtime = DesktopRuntime()
    result = runtime.execute(validated["action"], validated["payload"])
    return {
        "command_id": validated["command_id"],
        "action": validated["action"],
        "success": result["success"],
        "message": result["message"],
    }
