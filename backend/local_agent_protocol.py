"""Validation-only Local Agent protocol; desktop execution is intentionally disabled."""

import json
from typing import Any, Dict


MAX_MESSAGE_BYTES = 8192
ALLOWED_ACTIONS = {
    "open_app": {"app_id": {"calculator", "browser", "text_editor"}},
    "capture_screen": {},
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
    if command["execution_enabled"] is not False:
        raise ValueError("Desktop execution is disabled")

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
    elif payload:
        raise ValueError("Action does not accept a payload")

    return command


def handle_command(command: Any) -> Dict[str, Any]:
    """Validate a command and return a non-success result without performing it."""
    validated = validate_command(command)
    return {
        "command_id": validated["command_id"],
        "action": validated["action"],
        "success": False,
        "message": "Desktop execution is disabled",
    }
