import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop_runtime import DesktopRuntime
from local_agent_protocol import validate_command


class DesktopRuntimeTests(unittest.TestCase):
    def test_protocol_accepts_bounded_desktop_actions_and_rejects_extra_fields(self):
        for action, payload in [
            ("get_active_window", {}),
            ("capture_screen", {}),
            ("input_text", {"text": "hello"}),
            ("read_file", {"path": "notes/todo.txt"}),
            ("write_file", {"path": "notes/todo.txt", "content": "hello"}),
        ]:
            command = {
                "type": "command",
                "command_id": "command-a",
                "action": action,
                "payload": payload,
                "execution_enabled": True,
            }
            self.assertEqual(validate_command(command), command)

        invalid = {
            "type": "command",
            "command_id": "command-a",
            "action": "input_text",
            "payload": {"text": "x" * 501},
            "execution_enabled": True,
        }
        with self.assertRaises(ValueError):
            validate_command(invalid)

    def test_execution_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            runtime = DesktopRuntime()
            self.assertFalse(runtime.capabilities()["desktop_execution"])
            self.assertFalse(
                runtime.execute("open_app", {"app_id": "calculator"})["success"]
            )

    def test_restricted_filesystem_blocks_escape_credentials_and_oversize(self):
        with tempfile.TemporaryDirectory() as root:
            env = {
                "NEX_DESKTOP_EXECUTION_ENABLED": "true",
                "NEX_DESKTOP_EXECUTION_PROFILE": "approved",
                "NEX_DESKTOP_FS_ROOTS": root,
                "XDG_SESSION_TYPE": "x11",
            }
            with patch.dict(os.environ, env, clear=True):
                runtime = DesktopRuntime()
                escaped = runtime.execute(
                    "read_file",
                    {"path": "../outside.txt"},
                )
                self.assertFalse(escaped["success"])

                credential = runtime.execute(
                    "read_file",
                    {"path": ".ssh/private-key"},
                )
                self.assertFalse(credential["success"])

                (Path(root) / "bounded.txt").write_text("ok", encoding="utf-8")
                read = runtime.execute(
                    "read_file",
                    {"path": "bounded.txt"},
                )
                self.assertTrue(read["success"])

                oversized = runtime.execute(
                    "write_file",
                    {"path": "large.txt", "content": "x" * 65537},
                )
                self.assertFalse(oversized["success"])

    def test_emergency_stop_and_wayland_fail_closed(self):
        env = {
            "NEX_DESKTOP_EXECUTION_ENABLED": "true",
            "NEX_DESKTOP_EXECUTION_PROFILE": "approved",
            "XDG_SESSION_TYPE": "wayland",
        }
        with patch.dict(os.environ, env, clear=True):
            runtime = DesktopRuntime()
            active = runtime.execute("get_active_window", {})
            self.assertFalse(active["success"])
            typed = runtime.execute("input_text", {"text": "hello"})
            self.assertFalse(typed["success"])

            runtime.stop()
            app = runtime.execute("open_app", {"app_id": "calculator"})
            self.assertFalse(app["success"])

    def test_mocked_allowlisted_app_and_input_adapters(self):
        env = {
            "NEX_DESKTOP_EXECUTION_ENABLED": "true",
            "NEX_DESKTOP_EXECUTION_PROFILE": "approved",
            "XDG_SESSION_TYPE": "x11",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "desktop_runtime.shutil.which",
            side_effect=lambda name: (
                "/usr/bin/gnome-calculator"
                if name == "gnome-calculator"
                else "/usr/bin/xdotool"
                if name == "xdotool"
                else None
            ),
        ), patch("desktop_runtime.subprocess.Popen") as popen, patch(
            "desktop_runtime.subprocess.run"
        ) as run:
            runtime = DesktopRuntime()
            app = runtime.execute("open_app", {"app_id": "calculator"})
            typed = runtime.execute("input_text", {"text": "hello"})

            self.assertTrue(app["success"])
            popen.assert_called_once()
            self.assertTrue(typed["success"])
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
