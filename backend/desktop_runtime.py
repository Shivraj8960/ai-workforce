"""Fail-closed modular desktop runtime for the Local Agent."""

import hashlib
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Iterable


MAX_TEXT_CHARS = 500
MAX_FILE_BYTES = 65536

ACTION_SPECS = {
    "open_app": {"payload_keys": frozenset({"app_id"})},
    "capture_screen": {"payload_keys": frozenset()},
    "get_active_window": {"payload_keys": frozenset()},
    "input_text": {"payload_keys": frozenset({"text"})},
    "read_file": {"payload_keys": frozenset({"path"})},
    "write_file": {"payload_keys": frozenset({"path", "content"})},
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

    def stop(self) -> None:
        self._stopped.set()

    def reset(self) -> None:
        self._stopped.clear()

    def is_stopped(self) -> bool:
        return self._stopped.is_set()

    def capabilities(self) -> Dict[str, bool]:
        session = _session_type()
        x11 = session == "x11"
        wayland_capture = (
            session == "wayland"
            and os.getenv("NEX_DESKTOP_ALLOW_WAYLAND_CAPTURE", "").strip().lower()
            == "true"
            and shutil.which("grim") is not None
        )
        return {
            "open_app": desktop_execution_enabled(),
            "capture_screen": desktop_execution_enabled()
            and (x11 or wayland_capture),
            "get_active_window": desktop_execution_enabled() and x11
            and shutil.which("xdotool") is not None,
            "input_text": desktop_execution_enabled() and x11
            and shutil.which("xdotool") is not None,
            "read_file": desktop_execution_enabled() and bool(_filesystem_roots()),
            "write_file": desktop_execution_enabled() and bool(_filesystem_roots()),
            "desktop_execution": desktop_execution_enabled(),
            "x11": x11,
            "wayland": session == "wayland",
        }

    def execute(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not desktop_execution_enabled():
            return {"success": False, "message": "Desktop execution is disabled"}
        if self.is_stopped():
            return {"success": False, "message": "Desktop runtime emergency stop is active"}

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
        return {"success": False, "message": "Desktop action is unsupported"}

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
            capture_bytes = capture_file.read_bytes()
        except OSError:
            return {"success": False, "message": "Captured screen could not be read"}
        if len(capture_bytes) > 20 * 1024 * 1024:
            capture_file.unlink(missing_ok=True)
            return {"success": False, "message": "Captured screen exceeded the safety limit"}
        digest = hashlib.sha256(capture_bytes).hexdigest()
        capture_file.unlink(missing_ok=True)
        return {
            "success": True,
            "message": "Screen captured",
            "capture_sha256": digest,
            "capture_bytes": len(capture_bytes),
        }

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
