"""Strict Local Agent protocol and fail-closed desktop command validation."""

import json
import re
import re
from typing import Any, Dict


MAX_MESSAGE_BYTES = 8192
_SESSION_ID_PATTERN = re.compile(r"[a-f0-9-]{36}\Z")
ALLOWED_ACTIONS = {
    "open_app": {"app_id": {"calculator", "browser", "text_editor"}},
    "capture_screen": {},
    "get_active_window": {},
    "input_text": {"text": {"max_chars": 500}},
    "read_file": {"path": {"max_chars": 1024}},
    "write_file": {"path": {"max_chars": 1024}, "content": {"max_chars": 65536}},
    "click": {},
    "browser_navigate": {},
    "browser_read_page": {},
    "browser_inspect_controls": {},
    "browser_fill": {},
    "browser_submit": {},
    "browser_download": {},
    "browser_close": {},
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
    elif action == "capture_screen":
        if set(payload) - {"vision_provider", "redaction_boxes"}:
            raise ValueError("Screen analysis options are invalid")
        provider = payload.get("vision_provider", "mock")
        if not isinstance(provider, str) or provider not in {"mock", "openai"}:
            raise ValueError("Vision provider is invalid")
        if provider == "openai":
            boxes = payload.get("redaction_boxes")
            if not isinstance(boxes, list) or not 1 <= len(boxes) <= 32:
                raise ValueError("Remote vision requires screenshot redaction boxes")
        elif "redaction_boxes" in payload and not isinstance(payload["redaction_boxes"], list):
            raise ValueError("Screenshot redaction boxes are invalid")
    elif action == "get_active_window":
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
    elif action == "click":
        expectation = payload.get("expected_result")
        required_fields = {
            "x", "y", "screen_width", "screen_height", "observation_id",
            "expected_result", "device_id", "target_element_id", "target_bbox",
            "observation_captured_at", "observation_expires_at",
            "observation_image_sha256", "active_window_identity",
        }
        allowed_fields = required_fields | {"approval_ref"}
        if (
            frozenset(payload)
            not in {frozenset(required_fields), frozenset(allowed_fields)}
            or not isinstance(expectation, dict)
            or set(expectation) != {"kind", "text"}
            or expectation.get("kind") != "element_visible"
            or not isinstance(expectation.get("text"), str)
            or not 1 <= len(expectation["text"]) <= 200
        ):
            raise ValueError("Desktop click payload is invalid")
        if (
            any(
                isinstance(payload[key], bool) or not isinstance(payload[key], int)
                for key in ("x", "y", "screen_width", "screen_height")
            )
            or payload["screen_width"] < 1
            or payload["screen_height"] < 1
            or not 0 <= payload["x"] < payload["screen_width"]
            or not 0 <= payload["y"] < payload["screen_height"]
            or not isinstance(payload["observation_id"], str)
            or not payload["observation_id"]
            or len(payload["observation_id"]) > 64
            or not isinstance(payload["device_id"], str)
            or not payload["device_id"]
            or not isinstance(payload["target_element_id"], str)
            or not payload["target_element_id"]
            or not isinstance(payload["target_bbox"], dict)
            or set(payload["target_bbox"]) != {"x", "y", "width", "height"}
            or any(
                isinstance(payload["target_bbox"].get(key), bool)
                or not isinstance(payload["target_bbox"].get(key), int)
                for key in ("x", "y", "width", "height")
            )
            or payload["target_bbox"]["width"] < 1
            or payload["target_bbox"]["height"] < 1
            or payload["target_bbox"]["x"] < 0
            or payload["target_bbox"]["y"] < 0
            or payload["target_bbox"]["x"] + payload["target_bbox"]["width"]
            > payload["screen_width"]
            or payload["target_bbox"]["y"] + payload["target_bbox"]["height"]
            > payload["screen_height"]
            or payload["x"]
            != int(payload["target_bbox"]["x"] + payload["target_bbox"]["width"] / 2)
            or payload["y"]
            != int(payload["target_bbox"]["y"] + payload["target_bbox"]["height"] / 2)
            or not isinstance(payload["observation_captured_at"], str)
            or not isinstance(payload["observation_expires_at"], str)
            or not isinstance(payload["observation_image_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", payload["observation_image_sha256"])
            or not isinstance(payload["active_window_identity"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", payload["active_window_identity"])
            or (
                "approval_ref" in payload
                and (
                    not isinstance(payload["approval_ref"], str)
                    or not payload["approval_ref"]
                    or len(payload["approval_ref"]) > 64
                )
            )
        ):
            raise ValueError("Desktop click coordinates are invalid")
    elif action == "browser_navigate":
        if (
            set(payload) != {"session_id", "url"}
            or not isinstance(payload["session_id"], str)
            or not _SESSION_ID_PATTERN.fullmatch(payload["session_id"])
            or not isinstance(payload["url"], str)
            or not 1 <= len(payload["url"]) <= 2048
        ):
            raise ValueError("Browser navigation payload is invalid")
    elif action in {
        "browser_read_page",
        "browser_inspect_controls",
        "browser_close",
    }:
        if (
            set(payload) != {"session_id"}
            or not isinstance(payload["session_id"], str)
            or not _SESSION_ID_PATTERN.fullmatch(payload["session_id"])
        ):
            raise ValueError("Browser session payload is invalid")
    elif action == "browser_fill":
        if (
            set(payload)
            != {
                "session_id", "element_id", "text", "page_identity",
                "navigation_generation",
            }
            or not isinstance(payload["session_id"], str)
            or not _SESSION_ID_PATTERN.fullmatch(payload["session_id"])
            or not isinstance(payload["element_id"], str)
            or len(payload["element_id"]) > 64
            or not isinstance(payload["text"], str)
            or len(payload["text"]) > 2000
            or not isinstance(payload["page_identity"], str)
            or not _SESSION_ID_PATTERN.fullmatch(payload["page_identity"])
            or isinstance(payload["navigation_generation"], bool)
            or not isinstance(payload["navigation_generation"], int)
            or payload["navigation_generation"] < 1
        ):
            raise ValueError("Browser form-fill payload is invalid")
    elif action == "browser_submit":
        expectation = payload.get("expected_result")
        valid_expectation = (
            isinstance(expectation, dict)
            and (
                (
                    set(expectation) == {"kind", "url"}
                    and expectation["kind"] == "url"
                    and isinstance(expectation["url"], str)
                )
                or (
                    set(expectation) == {"kind", "text"}
                    and expectation["kind"] == "visible_text"
                    and isinstance(expectation["text"], str)
                    and 1 <= len(expectation["text"]) <= 500
                )
            )
        )
        if (
            set(payload)
            != {
                "session_id", "element_id", "expected_result", "page_identity",
                "navigation_generation",
            }
            or not isinstance(payload.get("session_id"), str)
            or not _SESSION_ID_PATTERN.fullmatch(payload["session_id"])
            or not isinstance(payload.get("element_id"), str)
            or len(payload["element_id"]) > 64
            or not valid_expectation
            or not isinstance(payload.get("page_identity"), str)
            or not _SESSION_ID_PATTERN.fullmatch(payload["page_identity"])
            or isinstance(payload.get("navigation_generation"), bool)
            or not isinstance(payload.get("navigation_generation"), int)
            or payload["navigation_generation"] < 1
        ):
            raise ValueError("Browser submission payload is invalid")
    elif action == "browser_download":
        if (
            set(payload)
            != {
                "session_id", "element_id", "page_identity",
                "navigation_generation",
            }
            or not isinstance(payload["session_id"], str)
            or not _SESSION_ID_PATTERN.fullmatch(payload["session_id"])
            or not isinstance(payload["element_id"], str)
            or len(payload["element_id"]) > 64
            or not isinstance(payload["page_identity"], str)
            or not _SESSION_ID_PATTERN.fullmatch(payload["page_identity"])
            or isinstance(payload["navigation_generation"], bool)
            or not isinstance(payload["navigation_generation"], int)
            or payload["navigation_generation"] < 1
        ):
            raise ValueError("Browser download payload is invalid")
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
    response = {
        "command_id": validated["command_id"],
        "action": validated["action"],
        "success": result["success"],
        "message": result["message"],
    }
    for key in (
        "session_id", "data", "observation", "verification_status",
        "observation_id", "page_identity", "navigation_generation",
        "session_closed", "failure_code", "failure_code",
        "active_window_identity", "failure_code",
    ):
        if key in result:
            response[key] = result[key]
    if "image_bytes" in result:
        image_bytes = result["image_bytes"]
        if not isinstance(image_bytes, bytes) or len(image_bytes) > 5 * 1024 * 1024:
            raise ValueError("Captured screenshot exceeds the safety limit")
        response["image_bytes"] = image_bytes
    return response
