"""Deterministic, observation-grounded action proposal construction."""

import json
import re

MIN_ACTION_CONFIDENCE = 0.85

OPERATION_ACTIONS = {
    "desktop.click": ("click", "click", "screen", "button"),
    "browser.navigate": ("browser_navigate", "browser_navigate", None, None),
    "browser.read_page": ("browser_read_page", "browser_read_page", None, None),
    "browser.inspect_controls": (
        "browser_inspect_controls",
        "browser_inspect_controls",
        None,
        None,
    ),
    "browser.fill": ("browser_fill", "browser_fill", "browser_dom", "text_field"),
    "browser.submit": ("browser_submit", "browser_submit", "browser_dom", "button"),
    "browser.download": ("browser_download", "browser_download", "browser_dom", "link"),
    "browser.close": ("browser_close", "browser_close", None, None),
}

SOURCE_OPERATIONS = {
    "screen": {"desktop.click"},
    "browser_dom": {"browser.fill", "browser.submit", "browser.download"},
}


def parse_agent_candidate(raw_response, allowed_operations):
    if not isinstance(raw_response, str) or len(raw_response) > 4000:
        raise ValueError("Computer Agent returned an invalid proposal")

    def reject_duplicate_keys(pairs):
        candidate = {}
        for key, value in pairs:
            if key in candidate:
                raise ValueError("Computer Agent returned duplicate JSON fields")
            candidate[key] = value
        return candidate

    def reject_constant(value):
        raise ValueError(f"Computer Agent returned invalid JSON value: {value}")

    try:
        candidate = json.loads(
            raw_response,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("Computer Agent did not return valid proposal JSON") from exc
    if (
        not isinstance(candidate, dict)
        or set(candidate) != {"operation", "target_element_id", "rationale"}
        or not isinstance(candidate.get("operation"), str)
        or candidate.get("operation") not in allowed_operations
        or not isinstance(candidate.get("target_element_id"), str)
        or len(candidate["target_element_id"]) > 64
        or not isinstance(candidate.get("rationale"), str)
        or not 1 <= len(candidate["rationale"]) <= 500
    ):
        raise ValueError("Computer Agent returned an invalid or unsupported proposal")
    return candidate


def build_action_proposal(
    operation,
    *,
    instruction=None,
    observation=None,
    target_element_id=None,
    session_id=None,
    url=None,
    text=None,
    expected_result=None,
):
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 1000:
        raise ValueError("A bounded user instruction is required")
    if operation not in OPERATION_ACTIONS:
        raise ValueError("Action proposal operation is unsupported")
    action, permission, observation_source, element_type = OPERATION_ACTIONS[operation]
    element = None
    confidence = 1.0
    if observation_source is not None:
        if not isinstance(observation, dict) or observation.get("source") != observation_source:
            raise ValueError("Action requires a matching observation")
        if not isinstance(target_element_id, str) or not target_element_id:
            raise ValueError("Target element is required")
        elements = observation.get("elements")
        if not isinstance(elements, list):
            raise ValueError("Observation elements are invalid")
        element = next(
            (item for item in elements if item.get("element_id") == target_element_id),
            None,
        )
        if element is None or element.get("type") != element_type:
            raise ValueError("Target element is not in the observation")
        confidence = element.get("confidence", 0)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or confidence < MIN_ACTION_CONFIDENCE
            or element.get("ambiguous") is not False
        ):
            raise ValueError("Target is ambiguous or below the action confidence threshold")

    if operation == "desktop.click":
        if (
            not isinstance(expected_result, dict)
            or set(expected_result) != {"kind", "text"}
            or expected_result.get("kind") != "element_visible"
            or not isinstance(expected_result.get("text"), str)
            or not 1 <= len(expected_result["text"]) <= 200
        ):
            raise ValueError("Desktop click requires an expected visible result")
        dimensions = observation.get("screen_dimensions")
        box = element.get("bounding_box")
        target_box_keys = {"x", "y", "width", "height"}
        image_sha256 = observation.get("image_sha256")
        active_window_identity = observation.get("active_window_identity")
        captured_at = observation.get("timestamp")
        expires_at = observation.get("expires_at")
        if (
            not isinstance(dimensions, dict)
            or not isinstance(box, dict)
            or set(box) != target_box_keys
            or any(
                isinstance(dimensions.get(key), bool)
                or not isinstance(dimensions.get(key), int)
                for key in ("width", "height")
            )
            or dimensions["width"] <= 0
            or dimensions["height"] <= 0
            or any(
                isinstance(box.get(key), bool) or not isinstance(box.get(key), int)
                for key in target_box_keys
            )
            or box["x"] < 0
            or box["y"] < 0
            or box["width"] <= 0
            or box["height"] <= 0
            or box["x"] + box["width"] > dimensions["width"]
            or box["y"] + box["height"] > dimensions["height"]
            or not isinstance(image_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", image_sha256)
            or not isinstance(active_window_identity, str)
            or not re.fullmatch(r"[a-f0-9]{64}", active_window_identity)
            or not isinstance(observation.get("device_id"), str)
            or not observation["device_id"]
            or not isinstance(captured_at, str)
            or not isinstance(expires_at, str)
        ):
            raise ValueError("Screen observation binding is invalid")
        return action, permission, confidence, {
            "x": int(box["x"] + box["width"] / 2),
            "y": int(box["y"] + box["height"] / 2),
            "screen_width": dimensions["width"],
            "screen_height": dimensions["height"],
            "observation_id": observation["observation_id"],
            "device_id": observation["device_id"],
            "target_element_id": target_element_id,
            "target_bbox": box,
            "observation_captured_at": captured_at,
            "observation_expires_at": expires_at,
            "observation_image_sha256": image_sha256,
            "active_window_identity": active_window_identity,
            "expected_result": expected_result,
        }

    if operation == "browser.navigate":
        if not isinstance(session_id, str) or not isinstance(url, str):
            raise ValueError("Browser navigation requires a session and URL")
        return action, permission, confidence, {"session_id": session_id, "url": url}

    if operation in {"browser.read_page", "browser.inspect_controls", "browser.close"}:
        if not isinstance(session_id, str):
            raise ValueError("Browser action requires a session")
        return action, permission, confidence, {"session_id": session_id}

    page_identity = observation.get("page_identity")
    navigation_generation = observation.get("navigation_generation")
    if (
        observation.get("browser_session_id") != session_id
        or not isinstance(page_identity, str)
        or len(page_identity) != 36
        or isinstance(navigation_generation, bool)
        or not isinstance(navigation_generation, int)
        or navigation_generation < 1
    ):
        raise ValueError("Browser observation is not bound to the active page")

    if operation == "browser.fill":
        if not isinstance(session_id, str) or not isinstance(text, str):
            raise ValueError("Browser form fill requires a session and text")
        return action, permission, confidence, {
            "session_id": session_id,
            "element_id": target_element_id,
            "text": text,
            "page_identity": page_identity,
            "navigation_generation": navigation_generation,
        }

    if operation == "browser.submit":
        if not isinstance(session_id, str) or not isinstance(expected_result, dict):
            raise ValueError("Browser submission requires a session and expected result")
        return action, permission, confidence, {
            "session_id": session_id,
            "element_id": target_element_id,
            "expected_result": expected_result,
            "page_identity": page_identity,
            "navigation_generation": navigation_generation,
        }

    if operation == "browser.download":
        if not isinstance(session_id, str):
            raise ValueError("Browser download requires a session")
        return action, permission, confidence, {
            "session_id": session_id,
            "element_id": target_element_id,
            "page_identity": page_identity,
            "navigation_generation": navigation_generation,
        }

    raise ValueError("Action proposal operation is unsupported")
