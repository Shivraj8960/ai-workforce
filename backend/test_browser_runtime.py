import os
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from browser_runtime import BrowserAutomationRuntime
from browser_runtime import MAX_DOWNLOAD_BYTES, MAX_REQUEST_BODY_BYTES
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.navigation_timeout = None
        self.goto_error = None
        self.main_frame = object()
        self.handlers = {}
        self.closed = False
        self.controls = []
        self.click_count = 0
        self.viewport_size = {"width": 100, "height": 80}

    def set_default_navigation_timeout(self, timeout):
        self.navigation_timeout = timeout

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def goto(self, url, **_kwargs):
        if self.goto_error is not None:
            raise self.goto_error
        self.url = url
        for callback in self.handlers.get("framenavigated", []):
            callback(self.main_frame)
        return SimpleNamespace(status=200)

    def reload(self):
        for callback in self.handlers.get("framenavigated", []):
            callback(self.main_frame)

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True
        for callback in self.handlers.get("close", []):
            callback()

    def locator(self, _selector):
        return FakeLocator(self)


class FakeElementHandle:
    def __init__(self, page, record):
        self.page = page
        self.record = record

    def evaluate(self, _script):
        return {
            "tag": self.record["tag"],
            "role": self.record["role"],
            "name": self.record["name"],
            "input_type": self.record["input_type"],
            "href": self.record["href"],
            "disabled": self.record["disabled"],
            "is_connected": any(item is self.record for item in self.page.controls),
            "x": self.record["x"],
            "y": self.record["y"],
            "width": self.record["width"],
            "height": self.record["height"],
        }

    def click(self, **_kwargs):
        if not any(item is self.record for item in self.page.controls):
            raise RuntimeError("detached element")
        self.page.click_count += 1
        self.record["clicked"] = True

    def fill(self, value, **_kwargs):
        self.record["value"] = value

    def select_option(self, value, **_kwargs):
        self.record["value"] = value

    def input_value(self, **_kwargs):
        return self.record.get("value", "")


class FakeLocator:
    def __init__(self, page, index=None):
        self.page = page
        self.index = index

    def evaluate_all(self, _script):
        return self.page.controls

    def nth(self, index):
        return FakeLocator(self.page, index)

    def count(self):
        return len(self.page.controls)

    def element_handle(self, **_kwargs):
        return FakeElementHandle(self.page, self.page.controls[self.index])

    def evaluate(self, _script):
        record = self.page.controls[self.index]
        return {
            "tag": record["tag"],
            "role": record["role"],
            "name": record["name"],
            "input_type": record["input_type"],
            "disabled": record["disabled"],
        }

    def click(self, **_kwargs):
        self.page.click_count += 1

    def fill(self, *_args, **_kwargs):
        return None

    def input_value(self, **_kwargs):
        return ""

class FakeContext:
    def __init__(self):
        self.route_handler = None
        self.route_pattern = None
        self.options = None
        self.page = FakePage()
        self.closed = False
        self.handlers = {}
        self.websocket_pattern = None
        self.websocket_handler = None

    def set_default_timeout(self, _timeout):
        return None

    def new_page(self):
        return self.page

    def route(self, pattern, handler):
        self.route_pattern = pattern
        self.route_handler = handler

    def route_web_socket(self, pattern, handler):
        self.websocket_pattern = pattern
        self.websocket_handler = handler

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def open_popup(self, page):
        for callback in self.handlers.get("page", []):
            callback(page)

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.context = FakeContext()
        self.launch_options = None
        self.context_options = None
        self.closed = False

    def new_context(self, **options):
        self.context_options = options
        return self.context

    def close(self):
        self.closed = True


class FakeManager:
    def __init__(self):
        self.browser = FakeBrowser()
        self.stopped = False
        self.chromium = self
        self.launch_options = None

    def start(self):
        return self

    def launch(self, **options):
        self.launch_options = options
        return self.browser

    def stop(self):
        self.stopped = True


class FakeRoute:
    def __init__(self, url, post_data=None, headers=None):
        self.request = SimpleNamespace(
            url=url,
            post_data_buffer=post_data,
            headers=headers or {},
        )
        self.aborted = False
        self.continued = False

    def abort(self, _reason):
        self.aborted = True

    def continue_(self):
        self.continued = True


class FakeWebSocketRoute:
    def __init__(self, url):
        self.url = url
        self.connected = False
        self.closed = False

    def connect_to_server(self):
        self.connected = True

    def close(self):
        self.closed = True


class FakeHTTPResponse:
    def __init__(self, chunks, status=200, headers=None, failure=None):
        self.chunks = iter(chunks)
        self.status = status
        self.headers = headers or {}
        self.failure = failure
        self.closed = False

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, _size):
        try:
            chunk = next(self.chunks)
        except StopIteration:
            if self.failure:
                raise OSError("connection interrupted")
            return b""
        return chunk

    def close(self):
        self.closed = True


class FakeHTTPConnection:
    def __init__(self, response):
        self.response = response
        self.requested = None
        self.closed = False

    def request(self, method, target, headers):
        self.requested = (method, target, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def public_resolver(host, _port, type):
    assert type == socket.SOCK_STREAM
    address = "93.184.216.34" if host == "example.com" else "127.0.0.1"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


class BrowserRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeManager()
        self.runtime = BrowserAutomationRuntime(
            resolver=public_resolver,
            playwright_factory=lambda: self.manager,
        )

    def test_isolated_browser_is_opt_in_pinned_and_redirect_checked(self):
        env = {
            "NEX_BROWSER_AUTOMATION_ENABLED": "true",
            "NEX_BROWSER_AUTOMATION_PROFILE": "approved",
            "NEX_BROWSER_ALLOWED_DOMAINS": "example.com",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "browser_runtime.importlib.util.find_spec", return_value=object()
        ):
            result = self.runtime.execute(
                "browser_navigate",
                {
                    "session_id": "12345678-1234-1234-1234-123456789abc",
                    "url": "https://example.com/",
                },
            )
            self.assertTrue(result["success"])
            self.assertEqual(result["verification_status"], "verified")
            self.assertEqual(self.manager.launch_options["headless"], True)
            rules = self.manager.launch_options["args"][0]
            self.assertIn("MAP example.com 93.184.216.34", rules)
            self.assertTrue(rules.endswith("MAP * ~NOTFOUND"))
            self.assertEqual(
                self.manager.browser.context_options,
                {"accept_downloads": True, "service_workers": "block"},
            )
            self.assertNotIn("storage_state", self.manager.browser.context_options)

            public_route = FakeRoute("https://example.com/redirect")
            self.manager.browser.context.route_handler(public_route)
            self.assertTrue(public_route.continued)
            private_redirect = FakeRoute("https://internal.example.com/")
            self.manager.browser.context.route_handler(private_redirect)
            self.assertTrue(private_redirect.aborted)
            oversized_request = FakeRoute(
                "https://example.com/submit",
                post_data=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
            )
            self.manager.browser.context.route_handler(oversized_request)
            self.assertTrue(oversized_request.aborted)

            self.runtime.stop()
            self.assertTrue(self.manager.browser.context.closed)
            self.assertTrue(self.manager.browser.closed)
            self.assertTrue(self.manager.stopped)

    def test_websocket_routes_allow_only_validated_secure_destinations(self):
        env = {
            "NEX_BROWSER_AUTOMATION_ENABLED": "true",
            "NEX_BROWSER_AUTOMATION_PROFILE": "approved",
            "NEX_BROWSER_ALLOWED_DOMAINS": "example.com",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "browser_runtime.importlib.util.find_spec", return_value=object()
        ):
            result = self.runtime.execute(
                "browser_navigate",
                {
                    "session_id": "12345678-1234-1234-1234-123456789abc",
                    "url": "https://example.com/",
                },
            )
            self.assertTrue(result["success"])
            self.assertEqual(self.manager.browser.context.websocket_pattern, "**/*")
            handler = self.manager.browser.context.websocket_handler
            permitted = FakeWebSocketRoute("wss://example.com/socket")
            handler(permitted)
            self.assertTrue(permitted.connected)
            for url in (
                "ws://example.com/socket",
                "wss://internal.example.test/socket",
                "wss://example.com:8443/socket",
            ):
                denied = FakeWebSocketRoute(url)
                handler(denied)
                self.assertTrue(denied.closed)

    def test_websocket_dns_change_after_session_creation_fails_closed(self):
        env = {
            "NEX_BROWSER_AUTOMATION_ENABLED": "true",
            "NEX_BROWSER_AUTOMATION_PROFILE": "approved",
            "NEX_BROWSER_ALLOWED_DOMAINS": "example.com",
        }
        addresses = ["93.184.216.34"]

        def resolver(host, _port, type):
            return [
                (socket.AF_INET, type, 6, "", (addresses[0], 443))
            ]

        runtime = BrowserAutomationRuntime(
            resolver=resolver,
            playwright_factory=lambda: self.manager,
        )
        with patch.dict(os.environ, env, clear=True), patch(
            "browser_runtime.importlib.util.find_spec", return_value=object()
        ):
            result = runtime.execute(
                "browser_navigate",
                {
                    "session_id": "12345678-1234-1234-1234-123456789abc",
                    "url": "https://example.com/",
                },
            )
            self.assertTrue(result["success"])
            addresses[0] = "127.0.0.1"
            denied = FakeWebSocketRoute("wss://example.com/socket")
            self.manager.browser.context.websocket_handler(denied)
            self.assertTrue(denied.closed)
            self.assertFalse(denied.connected)

    def test_stale_controls_are_rejected_after_navigation_reload_or_page_close(self):
        env = {
            "NEX_BROWSER_AUTOMATION_ENABLED": "true",
            "NEX_BROWSER_AUTOMATION_PROFILE": "approved",
            "NEX_BROWSER_ALLOWED_DOMAINS": "example.com",
        }
        self.manager.browser.context.page.controls = [
            {
                "index": 0,
                "tag": "button",
                "role": "button",
                "name": "Continue",
                "input_type": "",
                "href": "",
                "disabled": False,
                "x": 10,
                "y": 10,
                "width": 20,
                "height": 10,
            }
        ]
        session_id = "12345678-1234-1234-1234-123456789abc"
        with patch.dict(os.environ, env, clear=True), patch(
            "browser_runtime.importlib.util.find_spec", return_value=object()
        ):
            self.assertTrue(
                self.runtime.execute(
                    "browser_navigate",
                    {"session_id": session_id, "url": "https://example.com/"},
                )["success"]
            )
            observation = self.runtime.execute(
                "browser_inspect_controls", {"session_id": session_id}
            )["observation"]
            element_id = observation["elements"][0]["element_id"]
            stale_payload = {
                "session_id": session_id,
                "element_id": element_id,
                "expected_result": {"kind": "visible_text", "text": "Done"},
                "page_identity": observation["page_identity"],
                "navigation_generation": observation["navigation_generation"],
            }

            for change in (
                lambda: self.runtime.execute(
                    "browser_navigate",
                    {"session_id": session_id, "url": "https://example.com/next"},
                ),
                self.manager.browser.context.page.reload,
            ):
                change()
                result = self.runtime.execute("browser_submit", stale_payload)
                self.assertFalse(result["success"])
                self.assertIn("stale", result["message"])
                self.assertEqual(self.manager.browser.context.page.click_count, 0)
                self.runtime.execute("browser_inspect_controls", {"session_id": session_id})

            self.manager.browser.context.page.close()
            result = self.runtime.execute("browser_submit", stale_payload)
            self.assertFalse(result["success"])
            self.assertTrue(result["session_closed"])
            self.assertEqual(self.manager.browser.context.page.click_count, 0)

    def test_same_page_replacement_and_reorder_do_not_retarget_controls(self):
        env = {
            "NEX_BROWSER_AUTOMATION_ENABLED": "true",
            "NEX_BROWSER_AUTOMATION_PROFILE": "approved",
            "NEX_BROWSER_ALLOWED_DOMAINS": "example.com",
        }
        first = {
            "index": 0, "tag": "button", "role": "button", "name": "Continue",
            "input_type": "", "href": "", "disabled": False,
            "x": 10, "y": 10, "width": 20, "height": 10,
        }
        second = first | {"x": 50}
        page = self.manager.browser.context.page
        page.controls = [first, second]
        session_id = "12345678-1234-1234-1234-123456789abc"
        with patch.dict(os.environ, env, clear=True), patch(
            "browser_runtime.importlib.util.find_spec", return_value=object()
        ):
            self.runtime.execute(
                "browser_navigate",
                {"session_id": session_id, "url": "https://example.com/"},
            )
            observation = self.runtime.execute(
                "browser_inspect_controls", {"session_id": session_id}
            )["observation"]
            payload = {
                "session_id": session_id,
                "element_id": observation["elements"][0]["element_id"],
                "expected_result": {"kind": "visible_text", "text": "Done"},
                "page_identity": observation["page_identity"],
                "navigation_generation": observation["navigation_generation"],
            }

            replacement = first.copy()
            replacement["x"] = 40
            page.controls[0] = replacement
            result = self.runtime.execute("browser_submit", payload)
            self.assertFalse(result["success"])
            self.assertIn("changed", result["message"])
            self.assertEqual(page.click_count, 0)

            page.controls = [first, second]
            observation = self.runtime.execute(
                "browser_inspect_controls", {"session_id": session_id}
            )["observation"]
            payload["element_id"] = observation["elements"][0]["element_id"]
            payload["page_identity"] = observation["page_identity"]
            payload["navigation_generation"] = observation["navigation_generation"]
            page.controls.reverse()
            first["x"] = 30
            result = self.runtime.execute("browser_submit", payload)
            self.assertFalse(result["success"])
            self.assertIn("changed", result["message"])
            self.assertEqual(page.click_count, 0)

    def test_changed_link_destination_is_rejected_before_download(self):
        env = {
            "NEX_BROWSER_AUTOMATION_ENABLED": "true",
            "NEX_BROWSER_AUTOMATION_PROFILE": "approved",
            "NEX_BROWSER_ALLOWED_DOMAINS": "example.com",
        }
        link = {
            "index": 0, "tag": "a", "role": "a", "name": "Download report",
            "input_type": "", "href": "https://example.com/report.pdf",
            "disabled": False, "x": 10, "y": 10, "width": 20, "height": 10,
        }
        self.manager.browser.context.page.controls = [link]
        session_id = "12345678-1234-1234-1234-123456789abc"
        with patch.dict(os.environ, env, clear=True), patch(
            "browser_runtime.importlib.util.find_spec", return_value=object()
        ):
            self.runtime.execute(
                "browser_navigate",
                {"session_id": session_id, "url": "https://example.com/"},
            )
            observation = self.runtime.execute(
                "browser_inspect_controls", {"session_id": session_id}
            )["observation"]
            payload = {
                "session_id": session_id,
                "element_id": observation["elements"][0]["element_id"],
                "page_identity": observation["page_identity"],
                "navigation_generation": observation["navigation_generation"],
            }
            link["href"] = "https://example.com/other.pdf"
            with patch.object(self.runtime, "_bounded_https_download") as download:
                result = self.runtime.execute("browser_download", payload)
            self.assertFalse(result["success"])
            self.assertIn("changed", result["message"])
            download.assert_not_called()

    def test_download_is_bounded_during_stream_and_cleans_partial_files(self):
        env = {
            "NEX_BROWSER_ALLOWED_DOMAINS": "example.com",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            env | {"NEX_BROWSER_DOWNLOAD_DIR": temp_dir},
            clear=True,
        ):
            Path(temp_dir).chmod(0o700)

            oversized = FakeHTTPResponse([b"x" * (MAX_DOWNLOAD_BYTES + 1)])
            self.runtime._connection_factory = lambda *_args: FakeHTTPConnection(oversized)
            with self.assertRaisesRegex(ValueError, "exceeds the safety limit"):
                self.runtime._bounded_https_download(
                    "https://example.com/large.bin", ("example.com",)
                )
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

            interrupted = FakeHTTPResponse(
                [b"partial"],
                headers={"Content-Length": "20"},
                failure=True,
            )
            self.runtime._connection_factory = lambda *_args: FakeHTTPConnection(interrupted)
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.runtime._bounded_https_download(
                    "https://example.com/interrupted.bin", ("example.com",)
                )
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

            body = b"bounded file"
            successful = FakeHTTPResponse(
                [body[:4], body[4:]],
                headers={
                    "Content-Length": str(len(body)),
                    "Content-Disposition": 'attachment; filename="safe.txt"',
                },
            )
            self.runtime._connection_factory = lambda *_args: FakeHTTPConnection(successful)
            name, size = self.runtime._bounded_https_download(
                "https://example.com/file", ("example.com",)
            )
            self.assertEqual((name, size), ("safe.txt", len(body)))
            self.assertEqual((Path(temp_dir) / name).read_bytes(), body)
    def test_browser_sessions_are_isolated(self):
        env = {
            "NEX_BROWSER_AUTOMATION_ENABLED": "true",
            "NEX_BROWSER_AUTOMATION_PROFILE": "approved",
            "NEX_BROWSER_ALLOWED_DOMAINS": "example.com",
        }
        managers = [FakeManager(), FakeManager()]
        with patch.dict(os.environ, env, clear=True), patch(
            "browser_runtime.importlib.util.find_spec", return_value=object()
        ):
            runtime = BrowserAutomationRuntime(
                resolver=public_resolver,
                playwright_factory=iter(managers).__next__,
            )
            for session_id in (
                "12345678-1234-1234-1234-123456789abc",
                "22345678-1234-1234-1234-123456789abc",
            ):
                result = runtime.execute(
                    "browser_navigate",
                    {"session_id": session_id, "url": "https://example.com/"},
                )
                self.assertTrue(result["success"])
            self.assertIsNot(
                managers[0].browser.context,
                managers[1].browser.context,
            )
            self.assertEqual(len(runtime._sessions), 2)
            runtime.stop()

    def test_disabled_runtime_does_not_launch_and_private_dns_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            result = self.runtime.execute(
                "browser_navigate",
                {
                    "session_id": "12345678-1234-1234-1234-123456789abc",
                    "url": "https://example.com/",
                },
            )
            self.assertFalse(result["success"])
            self.assertEqual(self.manager.launch_options, None)

        env = {
            "NEX_BROWSER_AUTOMATION_ENABLED": "true",
            "NEX_BROWSER_AUTOMATION_PROFILE": "approved",
            "NEX_BROWSER_ALLOWED_DOMAINS": "example.com",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "browser_runtime.importlib.util.find_spec", return_value=object()
        ):
            blocked = BrowserAutomationRuntime(
                resolver=lambda *_args, **_kwargs: [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
                ],
                playwright_factory=lambda: self.manager,
            )
            result = blocked.execute(
                "browser_navigate",
                {
                    "session_id": "12345678-1234-1234-1234-123456789abc",
                    "url": "https://example.com/",
                },
            )
            self.assertFalse(result["success"])
            self.assertEqual(self.manager.launch_options, None)

    def test_browser_timeout_and_crash_return_failure_and_close_session(self):
        env = {
            "NEX_BROWSER_AUTOMATION_ENABLED": "true",
            "NEX_BROWSER_AUTOMATION_PROFILE": "approved",
            "NEX_BROWSER_ALLOWED_DOMAINS": "example.com",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "browser_runtime.importlib.util.find_spec", return_value=object()
        ):
            for failure in (
                PlaywrightTimeoutError("timed out"),
                PlaywrightError("browser crashed"),
            ):
                manager = FakeManager()
                manager.browser.context.page.goto_error = failure
                runtime = BrowserAutomationRuntime(
                    resolver=public_resolver,
                    playwright_factory=lambda: manager,
                )
                result = runtime.execute(
                    "browser_navigate",
                    {
                        "session_id": "12345678-1234-1234-1234-123456789abc",
                        "url": "https://example.com/",
                    },
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["verification_status"], "failed")
                self.assertEqual(runtime._sessions, {})
                self.assertTrue(manager.browser.context.closed)
                self.assertTrue(manager.browser.closed)
                self.assertTrue(manager.stopped)


if __name__ == "__main__":
    unittest.main()
