import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desktop_runtime import DesktopRuntime
from local_agent_protocol import validate_command


class DesktopRuntimeTests(unittest.TestCase):
    def click_payload(self):
        now = datetime.now(timezone.utc)
        return {
            "x": 25,
            "y": 25,
            "screen_width": 100,
            "screen_height": 80,
            "observation_id": "12345678-1234-1234-1234-123456789abc",
            "expected_result": {"kind": "element_visible", "text": "Done"},
            "device_id": "device-a",
            "target_element_id": "element-a",
            "target_bbox": {"x": 10, "y": 20, "width": 30, "height": 10},
            "observation_captured_at": (now - timedelta(minutes=1)).isoformat(),
            "observation_expires_at": (now + timedelta(minutes=5)).isoformat(),
            "observation_image_sha256": "a" * 64,
            "active_window_identity": "b" * 64,
            "approval_ref": "approval-a",
        }

    def run_mock_click(self, runtime, payload, fresh):
        env = {
            "NEX_DESKTOP_EXECUTION_ENABLED": "true",
            "NEX_DESKTOP_EXECUTION_PROFILE": "approved",
            "XDG_SESSION_TYPE": "x11",
        }
        calls = []

        def fake_run(args, **_kwargs):
            calls.append(args)
            return SimpleNamespace(stdout="100 80")

        with patch.dict(os.environ, env, clear=True), patch(
            "desktop_runtime.desktop_execution_enabled", return_value=True
        ), patch.object(runtime, "capabilities", return_value={"click": True}), patch(
            "desktop_runtime.shutil.which", return_value="/usr/bin/xdotool"
        ), patch.object(runtime, "_capture_screen", return_value=fresh), patch(
            "desktop_runtime.subprocess.run", side_effect=fake_run
        ), patch.object(
            runtime, "_active_window_identity", return_value="b" * 64
        ):
            result = runtime.execute("click", payload)
        return result, calls

    def fresh_capture(self, **overrides):
        return {
            "success": True,
            "capture_sha256": "a" * 64,
            "screen_width": 100,
            "screen_height": 80,
            "active_window_identity": "b" * 64,
        } | overrides

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

    def test_click_requires_unchanged_fresh_screen_and_active_window(self):
        cases = {
            "screen changed": {"capture_sha256": "c" * 64},
            "active window changed": {"active_window_identity": "d" * 64},
            "dimensions changed": {"screen_width": 1024},
            "target disappeared": {"capture_sha256": "c" * 64},
            "target moved": {"capture_sha256": "c" * 64},
            "fresh observation unavailable": {"success": False},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                runtime = DesktopRuntime()
                result, calls = self.run_mock_click(
                    runtime,
                    self.click_payload(),
                    self.fresh_capture(**overrides),
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["failure_code"], "desktop_click_stale")
                self.assertEqual(calls, [])

    def test_click_observation_expiry_and_forged_binding_fail_closed(self):
        expired_runtime = DesktopRuntime()
        expired = self.click_payload()
        expired["observation_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        result, calls = self.run_mock_click(
            expired_runtime, expired, self.fresh_capture()
        )
        self.assertFalse(result["success"])
        self.assertEqual(calls, [])

        forged_runtime = DesktopRuntime()
        forged = self.click_payload()
        forged["observation_id"] = "not-a-stored-observation"
        result, calls = self.run_mock_click(
            forged_runtime, forged, self.fresh_capture()
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["failure_code"], "desktop_click_stale")
        self.assertEqual(calls, [])

    def test_click_approval_is_single_use_and_unchanged_target_succeeds(self):
        runtime = DesktopRuntime()
        result, calls = self.run_mock_click(
            runtime, self.click_payload(), self.fresh_capture()
        )
        self.assertTrue(result["success"])
        self.assertEqual(len(calls), 2)

        repeated, repeat_calls = self.run_mock_click(
            runtime, self.click_payload(), self.fresh_capture()
        )
        self.assertFalse(repeated["success"])
        self.assertEqual(repeated["failure_code"], "desktop_click_stale")
        self.assertEqual(repeat_calls, [])


if __name__ == "__main__":
    unittest.main()
