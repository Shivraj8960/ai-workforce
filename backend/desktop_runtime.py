"""Fail-closed modular desktop runtime for the Local Agent."""

import hashlib
import io
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable


MAX_TEXT_CHARS = 500
MAX_FILE_BYTES = 65536
MAX_CAPTURE_BYTES = 5 * 1024 * 1024

ACTION_SPECS = {
    "open_app": {"payload_keys": frozenset({"app_id"})},
    "capture_screen": {"payload_keys": frozenset()},
    "get_active_window": {"payload_keys": frozenset()},
    "input_text": {"payload_keys": frozenset({"text"})},
    "read_file": {"payload_keys": frozenset({"path"})},
    "write_file": {"payload_keys": frozenset({"path", "content"})},
    "click": {
        "payload_keys": frozenset(
            {"x", "y", "screen_width", "screen_height", "observation_id"}
        )
    },
}

APP_EXECUTABLES = {
    "calculator": ("gnome-calculator", "kcalc", "galculator"),
    "browser": ("google-chrome", "chromium", "chromium-browser", "firefox"),
    "text_editor": ("gedit", "kate", "mousepad", "xed", "leafpad"),
}


def desktop_execution_enabled() -> bool:
    return (
        os.getenv("NEX_DESKTOP_EXECUTION_ENABLED", "").strip().lower() == "true"
        and os.getenv("NEX_DESKTOP_EXECUTION_PROFILE", "").strip().lower()
        == "approved"
        and not (hasattr(os, "geteuid") and os.geteuid() == 0)
    )


def _session_type() -> str:
    return os.getenv("XDG_SESSION_TYPE", "").strip().lower()


class DesktopRuntime:
    def __init__(self) -> None:
        self._stopped = threading.Event()
        self._browser_runtime = None
        self._click_lock = threading.Lock()
        self._used_click_approvals = set()

    def stop(self) -> None:
        self._stopped.set()
        if self._browser_runtime is not None:
            self._browser_runtime.stop()

    def reset(self) -> None:
        self._stopped.clear()

    def is_stopped(self) -> bool:
        return self._stopped.is_set()

    def capabilities(self) -> Dict[str, bool]:
        session = _session_type()
        x11 = session == "x11"
        xdotool_available = shutil.which("xdotool") is not None
        wayland_capture = (
            session == "wayland"
            and os.getenv("NEX_DESKTOP_ALLOW_WAYLAND_CAPTURE", "").strip().lower()
            == "true"
            and shutil.which("grim") is not None
        )
        capabilities = {
            "open_app": desktop_execution_enabled(),
            "capture_screen": desktop_execution_enabled()
            and (x11 or wayland_capture),
            "get_active_window": desktop_execution_enabled() and x11
            and xdotool_available,
            "input_text": desktop_execution_enabled() and x11
            and xdotool_available,
            "click": desktop_execution_enabled() and x11 and xdotool_available,
            "read_file": desktop_execution_enabled() and bool(_filesystem_roots()),
            "write_file": desktop_execution_enabled() and bool(_filesystem_roots()),
            "desktop_execution": desktop_execution_enabled(),
            "x11": x11,
            "wayland": session == "wayland",
        }
        capabilities.update(self._browser().capabilities())
        return capabilities

    def execute(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.is_stopped():
            return {"success": False, "message": "Desktop runtime emergency stop is active"}

        if action.startswith("browser_"):
            return self._browser().execute(action, payload)

        if not desktop_execution_enabled():
            return {"success": False, "message": "Desktop execution is disabled"}
        capabilities = self.capabilities()
        if action not in ACTION_SPECS or not capabilities.get(action, False):
            return {"success": False, "message": "Desktop capability is unavailable"}

        if action == "open_app":
            return self._open_app(payload["app_id"])
        if action == "capture_screen":
            return self._capture_screen()
        if action == "get_active_window":
            return self._get_active_window()
        if action == "input_text":
            return self._input_text(payload["text"])
        if action == "read_file":
            return self._read_file(payload["path"])
        if action == "write_file":
            return self._write_file(payload["path"], payload["content"])
        if action == "click":
            return self._click(payload)
        return {"success": False, "message": "Desktop action is unsupported"}

    def _browser(self):
        if self._browser_runtime is None:
            from browser_runtime import BrowserAutomationRuntime

            self._browser_runtime = BrowserAutomationRuntime()
        return self._browser_runtime

    def _open_app(self, app_id: str) -> Dict[str, Any]:
        executable = None
        for candidate in APP_EXECUTABLES.get(app_id, ()):
            executable = shutil.which(candidate)
            if executable:
                break
        if executable is None:
            return {"success": False, "message": "Allowlisted application is unavailable"}
        try:
            subprocess.Popen(
                [executable],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return {"success": False, "message": "Allowlisted application could not start"}
        return {"success": True, "message": "Allowlisted application started"}

    def _capture_screen(self) -> Dict[str, Any]:
        output_dir = os.getenv("NEX_DESKTOP_CAPTURE_DIR", "").strip()
        if not output_dir:
            return {"success": False, "message": "Screen capture directory is not configured"}
        directory = Path(output_dir).expanduser().resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_mode & 0o077:
            return {"success": False, "message": "Screen capture directory permissions are unsafe"}
        capture_file = directory / "capture.png"
        try:
            window_before = self._active_window_identity()
            session = _session_type()
            if session == "x11":
                screenshot = shutil.which("gnome-screenshot")
                if screenshot is None:
                    try:
                        from PIL import ImageGrab

                        image = ImageGrab.grab()
                        image.save(str(capture_file), "PNG")
                    except Exception:
                        return {"success": False, "message": "Screen capture adapter is unavailable"}
                else:
                    subprocess.run(
                        [screenshot, "-f", str(capture_file)],
                        check=True,
                        timeout=10,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            else:
                grim = shutil.which("grim")
                if grim is None:
                    return {"success": False, "message": "Wayland screen capture adapter is unavailable"}
                subprocess.run(
                    [grim, str(capture_file)],
                    check=True,
                    timeout=10,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except (OSError, subprocess.SubprocessError):
            return {"success": False, "message": "Screen capture failed"}

        try:
            if capture_file.stat().st_size > MAX_CAPTURE_BYTES:
                capture_file.unlink(missing_ok=True)
                return {"success": False, "message": "Captured screen exceeded the safety limit"}
            capture_bytes = capture_file.read_bytes()
        except OSError:
            capture_file.unlink(missing_ok=True)
            return {"success": False, "message": "Captured screen could not be read"}
        window_after = self._active_window_identity()
        if window_before != window_after:
            capture_file.unlink(missing_ok=True)
            return {"success": False, "message": "Active window changed during screen capture"}
        try:
            from PIL import Image

            with Image.open(io.BytesIO(capture_bytes)) as image:
                width, height = image.size
        except (ImportError, OSError, ValueError):
            capture_file.unlink(missing_ok=True)
            return {"success": False, "message": "Captured screen dimensions are unavailable"}
        digest = hashlib.sha256(capture_bytes).hexdigest()
        capture_file.unlink(missing_ok=True)
        return {
            "success": True,
            "message": "Screen captured",
            "capture_sha256": digest,
            "capture_bytes": len(capture_bytes),
            "screen_width": width,
            "screen_height": height,
            "active_window_identity": window_after,
            "image_bytes": capture_bytes,
        }

    def _active_window_identity(self):
        xdotool = shutil.which("xdotool")
        if xdotool is None or _session_type() != "x11":
            return None
        try:
            result = subprocess.run(
                [xdotool, "getactivewindow"],
                check=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        window_id = result.stdout.strip()
        if not window_id or not window_id.isdigit():
            return None
        return hashlib.sha256(window_id.encode("ascii")).hexdigest()

    def _get_active_window(self) -> Dict[str, Any]:
        xdotool = shutil.which("xdotool")
        if xdotool is None:
            return {"success": False, "message": "Active-window adapter is unavailable"}
        try:
            result = subprocess.run(
                [xdotool, "getactivewindow", "getwindowname"],
                check=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return {"success": False, "message": "Active window could not be read"}
        title = result.stdout.strip()
        if not title:
            return {"success": False, "message": "Active window title is unavailable"}
        return {"success": True, "message": "Active window read", "window_title": title[:200]}

    def _input_text(self, text: str) -> Dict[str, Any]:
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT_CHARS:
            return {"success": False, "message": "Input text is outside the safety limit"}
        xdotool = shutil.which("xdotool")
        if xdotool is None:
            return {"success": False, "message": "Input adapter is unavailable"}
        try:
            subprocess.run(
                [xdotool, "type", "--clearmodifiers", "--delay", "5", text],
                check=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return {"success": False, "message": "Approved input could not be sent"}
        return {"success": True, "message": "Approved text input sent"}

    def _click(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        approval_ref = payload.get("approval_ref")
        if not isinstance(approval_ref, str) or not approval_ref:
            return self._stale_click("Desktop click approval binding is missing")
        with self._click_lock:
            if approval_ref in self._used_click_approvals:
                return self._stale_click("Approved desktop click was already attempted")
            self._used_click_approvals.add(approval_ref)

        binding = (
            payload.get("device_id"),
            payload.get("observation_id"),
            payload.get("target_element_id"),
            payload.get("observation_captured_at"),
            payload.get("observation_expires_at"),
            payload.get("observation_image_sha256"),
            payload.get("active_window_identity"),
        )
        if (
            any(not isinstance(value, str) or not value for value in binding)
            or not re.fullmatch(
                r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}",
                payload["observation_id"]
                if isinstance(payload.get("observation_id"), str)
                else "",
            )
            or not all(
                isinstance(payload.get(key), int) and not isinstance(payload.get(key), bool)
                for key in ("screen_width", "screen_height", "x", "y")
            )
            or not isinstance(payload.get("target_bbox"), dict)
        ):
            return self._stale_click("Desktop click observation binding is invalid")

        try:
            expires_at = datetime.fromisoformat(payload["observation_expires_at"])
            if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
                return self._stale_click("Desktop click observation expired")
        except ValueError:
            return self._stale_click("Desktop click observation timestamp is invalid")

        values = (
            payload.get("x"),
            payload.get("y"),
            payload.get("screen_width"),
            payload.get("screen_height"),
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            return self._stale_click("Approved click coordinates are invalid")
        x, y, expected_width, expected_height = values
        box = payload["target_bbox"]
        if (
            set(box) != {"x", "y", "width", "height"}
            or any(
                isinstance(box.get(key), bool) or not isinstance(box.get(key), int)
                for key in ("x", "y", "width", "height")
            )
            or box["width"] <= 0
            or box["height"] <= 0
            or (x, y)
            != (
                int(box["x"] + box["width"] / 2),
                int(box["y"] + box["height"] / 2),
            )
            or box["x"] < 0
            or box["y"] < 0
            or box["x"] + box["width"] > expected_width
            or box["y"] + box["height"] > expected_height
        ):
            return self._stale_click("Approved desktop target geometry is invalid")
        xdotool = shutil.which("xdotool")
        if xdotool is None:
            return self._stale_click("Fresh desktop observation is unavailable")
        fresh = self._capture_screen()
        if not fresh.get("success"):
            return self._stale_click("Fresh desktop observation is unavailable")
        if (
            fresh.get("screen_width") != expected_width
            or fresh.get("screen_height") != expected_height
            or fresh.get("capture_sha256") != payload["observation_image_sha256"]
            or fresh.get("active_window_identity") != payload["active_window_identity"]
        ):
            return self._stale_click("Desktop target changed after approval; create a new proposal")
        try:
            display = subprocess.run(
                [xdotool, "getdisplaygeometry"],
                check=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            ).stdout.split()
            if (
                len(display) != 2
                or int(display[0]) != expected_width
                or int(display[1]) != expected_height
                or not 0 <= x < expected_width
                or not 0 <= y < expected_height
            ):
                return self._stale_click(
                    "Screen changed after approval; create a new proposal"
                )
            if self._active_window_identity() != payload["active_window_identity"]:
                return self._stale_click(
                    "Active window changed after approval; create a new proposal"
                )
            subprocess.run(
                [xdotool, "mousemove", "--sync", str(x), str(y), "click", "1"],
                check=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return {
                "success": False,
                "message": "Approved click could not be sent; do not retry",
                "failure_code": "desktop_click_stale",
            }
        return {
            "success": True,
            "message": "Approved click sent",
            "verification_status": "unverified",
        }

    @staticmethod
    def _stale_click(message):
        return {
            "success": False,
            "message": message,
            "failure_code": "desktop_click_stale",
        }

    def _read_file(self, relative_path: str) -> Dict[str, Any]:
        try:
            path = _resolve_restricted_path(relative_path)
            if not path.is_file():
                raise ValueError("Restricted file does not exist")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Restricted file exceeds the size limit")
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return {"success": False, "message": str(exc)}
        return {
            "success": True,
            "message": "Restricted file read",
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content_bytes": len(content.encode("utf-8")),
        }

    def _write_file(self, relative_path: str, content: str) -> Dict[str, Any]:
        if not isinstance(content, str):
            return {"success": False, "message": "File content is invalid"}
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            return {"success": False, "message": "File content exceeds the size limit"}
        try:
            path = _resolve_restricted_path(relative_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except (OSError, UnicodeEncodeError, ValueError) as exc:
            return {"success": False, "message": str(exc)}
        return {
            "success": True,
            "message": "Restricted file written",
            "content_sha256": hashlib.sha256(encoded).hexdigest(),
            "content_bytes": len(encoded),
        }


def _filesystem_roots() -> Iterable[Path]:
    raw = os.getenv("NEX_DESKTOP_FS_ROOTS", "").strip()
    if not raw:
        return ()
    roots = []
    for item in raw.split(os.pathsep):
        if item:
            roots.append(Path(item).expanduser().resolve())
    return tuple(roots)


def _resolve_restricted_path(relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("Restricted file path is invalid")
    if os.path.isabs(relative_path):
        raise ValueError("Absolute restricted file paths are forbidden")
    roots = _filesystem_roots()
    if not roots:
        raise ValueError("Restricted filesystem roots are not configured")

    candidate = (roots[0] / relative_path).resolve()
    if not any(
        candidate == root or root in candidate.parents
        for root in roots
    ):
        raise ValueError("Restricted file path escapes the allowed roots")

    denied_parts = {
        ".ssh", ".gnome", ".config", ".local", "credentials",
        "credential", "secrets", "keyring", "password", "passwd",
    }
    if denied_parts.intersection(part.lower() for part in candidate.parts):
        raise ValueError("Credential or profile paths are forbidden")
    if candidate.name.lower() in {".env", ".netrc", "credentials.json"}:
        raise ValueError("Credential or profile files are forbidden")
    return candidate
