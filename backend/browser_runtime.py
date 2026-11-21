"""Opt-in, isolated Playwright browser adapter for the Local Agent."""

import http.client
import importlib.util
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
import threading
import time
from typing import Any
from email.message import Message
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
import uuid

from browser_security import (
    build_host_resolver_rules,
    configured_browser_domains,
    validate_browser_url,
    validate_browser_websocket_url,
)


MAX_BROWSER_TEXT_CHARS = 6000
MAX_BROWSER_CONTROLS = 10
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
NAVIGATION_TIMEOUT_MS = 15_000
MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024
MAX_ACTIVE_SESSIONS = 3
SESSION_IDLE_TTL_SECONDS = 900
CONTROL_SELECTOR = "button,a,input,textarea,select,[role=button]"


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def browser_automation_enabled() -> bool:
    return (
        os.getenv("NEX_BROWSER_AUTOMATION_ENABLED", "").strip().lower() == "true"
        and os.getenv("NEX_BROWSER_AUTOMATION_PROFILE", "").strip().lower() == "approved"
        and not (hasattr(os, "geteuid") and os.geteuid() == 0)
    )


def _session_identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9-]{36}", value):
        raise ValueError("Browser session ID is invalid")
    return value


def _browser_imports():
    if importlib.util.find_spec("playwright") is None:
        raise RuntimeError("Playwright is not installed for the Local Agent")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    return sync_playwright, PlaywrightError, PlaywrightTimeoutError


class BrowserAutomationRuntime:
    def __init__(self, resolver=None, playwright_factory=None) -> None:
        self._resolver = resolver
        self._playwright_factory = playwright_factory
        self._connection_factory = None
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def capabilities(self) -> dict[str, bool]:
        enabled = browser_automation_enabled() and importlib.util.find_spec("playwright") is not None
        return {
            "browser_automation": enabled,
            "browser_navigate": enabled,
            "browser_read_page": enabled,
            "browser_inspect_controls": enabled,
            "browser_fill": enabled,
            "browser_submit": enabled,
            "browser_download": enabled,
            "browser_close": enabled,
        }

    def execute(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not browser_automation_enabled():
            return {"success": False, "message": "Browser automation is disabled"}
        if action not in self.capabilities() or not self.capabilities()[action]:
            return {"success": False, "message": "Browser capability is unavailable"}
        try:
            with self._lock:
                self._expire_idle_sessions()
                if action == "browser_navigate":
                    return self._navigate(payload)
                if action == "browser_read_page":
                    return self._read_page(payload)
                if action == "browser_inspect_controls":
                    return self._inspect_controls(payload)
                if action == "browser_fill":
                    return self._fill(payload)
                if action == "browser_submit":
                    return self._submit(payload)
                if action == "browser_download":
                    return self._download(payload)
                if action == "browser_close":
                    return self._close_session(payload)
        except ValueError as exc:
            result = {"success": False, "message": str(exc)}
            session_id = payload.get("session_id")
            session = self._sessions.get(session_id) if isinstance(session_id, str) else None
            if session is not None:
                result["page_identity"] = session["page_identity"]
                result["navigation_generation"] = session["navigation_generation"]
                if session["closed"] or session["page"].is_closed():
                    result["session_closed"] = True
                    session["closed"] = True
                    self._close_one(session_id)
            return result
        except OSError:
            return {"success": False, "message": "Browser filesystem operation failed"}
        except Exception as exc:
            try:
                _, browser_error, browser_timeout = _browser_imports()
            except (ImportError, RuntimeError):
                raise
            if not isinstance(exc, (browser_error, browser_timeout)):
                raise
            session_id = payload.get("session_id")
            if isinstance(session_id, str):
                session = self._sessions.get(session_id)
                state = {}
                if session is not None:
                    state = {
                        "page_identity": session["page_identity"],
                        "navigation_generation": session["navigation_generation"],
                        "session_closed": True,
                    }
                self._close_one(session_id)
            else:
                state = {}
            message = (
                "Browser action timed out; session closed"
                if isinstance(exc, browser_timeout)
                else "Browser runtime failed; session closed"
            )
            return {
                "success": False,
                "message": message,
                "verification_status": "failed",
                **state,
            }
        return {"success": False, "message": "Browser action is unsupported"}

    def _allowed_domains(self):
        return configured_browser_domains()

    def _resolve(self, url: str, domains: tuple[str, ...]):
        if self._resolver is None:
            return validate_browser_url(url, domains)
        return validate_browser_url(url, domains, resolver=self._resolver)

    def _resolve_websocket(self, url: str, domains: tuple[str, ...]):
        if self._resolver is None:
            return validate_browser_websocket_url(url, domains)
        return validate_browser_websocket_url(url, domains, resolver=self._resolver)

    def _watch_page(self, context, page, session):
        def main_frame_navigated(frame):
            if frame == page.main_frame:
                session["navigation_generation"] += 1
                session["controls"] = {}

        def page_closed():
            session["closed"] = True
            session["controls"] = {}

        page.on("framenavigated", main_frame_navigated)
        page.on("close", page_closed)
        context.on(
            "page",
            lambda opened_page: opened_page.close() if opened_page != page else None,
        )

        def guard_websocket(route):
            try:
                self._resolve_websocket(route.url, session["domains"])
            except ValueError:
                route.close()
                return
            route.connect_to_server()

        if not hasattr(context, "route_web_socket"):
            raise RuntimeError("Playwright WebSocket interception is unavailable")
        context.route_web_socket("**/*", guard_websocket)

    def _new_session(self, session_id: str, initial_url: str):
        if len(self._sessions) >= MAX_ACTIVE_SESSIONS:
            raise ValueError("Local Agent browser session limit reached")
        domains = self._allowed_domains()
        normalized_url, _ = self._resolve(initial_url, domains)
        rules = (
            build_host_resolver_rules(domains)
            if self._resolver is None
            else build_host_resolver_rules(domains, resolver=self._resolver)
        )
        factory = self._playwright_factory
        if factory is None:
            factory, _, _ = _browser_imports()
        manager = factory()
        playwright = manager.start()
        browser = None
        try:
            browser = playwright.chromium.launch(
                headless=True,
                args=[f"--host-resolver-rules={rules}"],
            )
            context = browser.new_context(
                accept_downloads=True,
                service_workers="block",
            )
            context.set_default_timeout(5000)
            page = context.new_page()
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

            def guard_request(route):
                try:
                    self._resolve(route.request.url, domains)
                except ValueError:
                    route.abort("blockedbyclient")
                    return
                post_data = route.request.post_data_buffer
                headers = route.request.headers
                if (
                    (post_data is not None and len(post_data) > MAX_REQUEST_BODY_BYTES)
                    or sum(len(key) + len(value) for key, value in headers.items()) > 64 * 1024
                ):
                    route.abort("blockedbyclient")
                    return
                route.continue_()

            context.route("**/*", guard_request)
            session = {
                "manager": manager,
                "playwright": playwright,
                "browser": browser,
                "context": context,
                "page": page,
                "domains": domains,
                "controls": {},
                "last_used": time.monotonic(),
                "page_identity": str(uuid.uuid4()),
                "navigation_generation": 0,
                "closed": False,
            }
            self._watch_page(context, page, session)
            self._sessions[session_id] = session
            response = page.goto(
                normalized_url,
                wait_until="domcontentloaded",
                timeout=NAVIGATION_TIMEOUT_MS,
            )
            final_url, _ = self._resolve(page.url, domains)
            if response is not None and response.status >= 400:
                self._close_one(session_id)
                return {"success": False, "message": "Browser navigation returned an error"}
            return {
                "success": True,
                "message": "Approved browser session navigated",
                "session_id": session_id,
                "data": {"url": final_url},
                "verification_status": "verified",
                "page_identity": session["page_identity"],
                "navigation_generation": session["navigation_generation"],
            }
        except Exception:
            if session_id in self._sessions:
                self._close_one(session_id)
            else:
                if browser is not None:
                    browser.close()
                manager.stop()
            raise

    def _session(self, payload):
        session_id = _session_identifier(payload.get("session_id"))
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("Browser session is unavailable or expired")
        if session["closed"] or session["page"].is_closed():
            session["closed"] = True
            session["controls"] = {}
            raise ValueError("Browser page was replaced or closed")
        if time.monotonic() - session["last_used"] > SESSION_IDLE_TTL_SECONDS:
            self._close_one(session_id)
            raise ValueError("Browser session is unavailable or expired")
        session["last_used"] = time.monotonic()
        try:
            self._resolve(session["page"].url, session["domains"])
        except ValueError as exc:
            self._close_one(session_id)
            raise ValueError("Browser session destination is no longer authorized") from exc
        return session_id, session

    def _expire_idle_sessions(self):
        now = time.monotonic()
        for session_id, session in list(self._sessions.items()):
            if now - session["last_used"] > SESSION_IDLE_TTL_SECONDS:
                self._close_one(session_id)

    def _navigate(self, payload):
        if set(payload) != {"session_id", "url"} or not isinstance(payload.get("url"), str):
            raise ValueError("Browser navigation payload is invalid")
        session_id = _session_identifier(payload["session_id"])
        session = self._sessions.get(session_id)
        if session is None:
            return self._new_session(session_id, payload["url"])
        if session["closed"] or session["page"].is_closed():
            session["closed"] = True
            session["controls"] = {}
            raise ValueError("Browser page was replaced or closed")
        url, _ = self._resolve(payload["url"], session["domains"])
        session["navigation_generation"] += 1
        session["controls"] = {}
        response = session["page"].goto(
            url,
            wait_until="domcontentloaded",
            timeout=NAVIGATION_TIMEOUT_MS,
        )
        final_url, _ = self._resolve(session["page"].url, session["domains"])
        if response is not None and response.status >= 400:
            return {"success": False, "message": "Browser navigation returned an error"}
        return {
            "success": True,
            "message": "Approved browser session navigated",
            "session_id": session_id,
            "data": {"url": final_url},
            "verification_status": "verified",
            "page_identity": session["page_identity"],
            "navigation_generation": session["navigation_generation"],
        }

    def _read_page(self, payload):
        if set(payload) != {"session_id"}:
            raise ValueError("Browser read payload is invalid")
        session_id, session = self._session(payload)
        text = session["page"].locator("body").inner_text(timeout=5000)
        text = text[:MAX_BROWSER_TEXT_CHARS]
        return {
            "success": True,
            "message": "Visible page text read",
            "session_id": session_id,
            "data": {"url": session["page"].url, "visible_text": text},
            "verification_status": "observed",
            "page_identity": session["page_identity"],
            "navigation_generation": session["navigation_generation"],
        }

    def _inspect_controls(self, payload):
        if set(payload) != {"session_id"}:
            raise ValueError("Browser inspection payload is invalid")
        session_id, session = self._session(payload)
        controls_locator = session["page"].locator(CONTROL_SELECTOR)
        records = []
        for index in range(min(controls_locator.count(), MAX_BROWSER_CONTROLS)):
            element = controls_locator.nth(index).element_handle(timeout=1000)
            if element is None:
                continue
            record = self._read_control(element)
            record["index"] = index
            record["element_handle"] = element
            records.append(record)
        controls = {}
        elements = []
        width = session["page"].viewport_size["width"]
        height = session["page"].viewport_size["height"]
        for record in records:
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("name"), str)
                or not record["is_connected"]
                or record["disabled"]
                or record["input_type"] in {"password", "hidden", "file"}
                or record["width"] <= 0
                or record["height"] <= 0
                or record["x"] < 0
                or record["y"] < 0
                or record["x"] + record["width"] > width
                or record["y"] + record["height"] > height
            ):
                continue
            group = "fields" if record["tag"] in {"input", "textarea", "select"} else "buttons"
            element_id = "control-" + uuid.uuid4().hex
            controls[element_id] = record | {
                "group": group,
                "page_identity": session["page_identity"],
                "navigation_generation": session["navigation_generation"],
            }
            control_type = "text_field" if group == "fields" else (
                "link" if record["tag"] == "a" else "button"
            )
            elements.append(
                {
                    "element_id": element_id,
                    "type": control_type,
                    "description": record["name"] or f"{record['role']} control",
                    "bounding_box": {
                        "x": record["x"],
                        "y": record["y"],
                        "width": record["width"],
                        "height": record["height"],
                    },
                    "confidence": 1.0,
                    "ambiguous": False,
                    "control": {
                        "index": record["index"],
                        "tag": record["tag"],
                        "role": record["role"],
                        "input_type": record["input_type"],
                    },
                }
            )
        session["controls"] = controls
        page_identity = session["page_identity"]
        navigation_generation = session["navigation_generation"]
        return {
            "success": True,
            "message": "Visible browser controls inspected",
            "session_id": session_id,
            "observation": {
                "source": "browser_dom",
                "screen_width": width,
                "screen_height": height,
                "elements": elements,
                "browser_session_id": session_id,
                "page_identity": page_identity,
                "navigation_generation": navigation_generation,
            },
            "verification_status": "observed",
            "page_identity": page_identity,
            "navigation_generation": navigation_generation,
        }

    def _read_control(self, element):
        return element.evaluate(
            """el => {
            const rect = el.getBoundingClientRect();
            const tag = el.tagName.toLowerCase();
            const role = el.getAttribute("role") || tag;
            const name = (
                el.getAttribute("aria-label") ||
                el.getAttribute("title") ||
                (tag === "input" ? "" : (el.innerText || "")) ||
                ""
            ).trim().slice(0, 80);
            return {
                tag,
                role: role.slice(0, 40),
                name,
                input_type: (el.getAttribute("type") || "").toLowerCase(),
                href: el.href || "",
                disabled: Boolean(el.disabled),
                is_connected: el.isConnected,
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height
            };
            }"""
        )

    def _control(self, session, payload, expected_group):
        element_id = payload.get("element_id")
        if not isinstance(element_id, str) or len(element_id) > 64:
            raise ValueError("Browser target element is invalid")
        if (
            payload.get("page_identity") != session["page_identity"]
            or payload.get("navigation_generation") != session["navigation_generation"]
            or session["closed"]
            or session["page"].is_closed()
        ):
            session["controls"] = {}
            raise ValueError("Browser proposal is stale; inspect controls again")
        record = session["controls"].get(element_id)
        if (
            record is None
            or record["group"] != expected_group
            or record["page_identity"] != session["page_identity"]
            or record["navigation_generation"] != session["navigation_generation"]
        ):
            raise ValueError("Browser target is not in the latest observation")
        element = record["element_handle"]
        current = self._read_control(element)
        if (
            not current["is_connected"]
            or current["tag"] != record["tag"]
            or current["role"] != record["role"]
            or current["name"] != record["name"]
            or current["input_type"] != record["input_type"]
            or current["href"] != record["href"]
            or current["x"] != record["x"]
            or current["y"] != record["y"]
            or current["width"] != record["width"]
            or current["height"] != record["height"]
            or current["disabled"]
        ):
            raise ValueError("Browser target changed after observation; create a new proposal")
        if (
            payload.get("page_identity") != session["page_identity"]
            or payload.get("navigation_generation") != session["navigation_generation"]
            or session["closed"]
            or session["page"].is_closed()
        ):
            raise ValueError("Browser proposal became stale before execution")
        return element, record

    def _fill(self, payload):
        if set(payload) != {
            "session_id", "element_id", "text", "page_identity",
            "navigation_generation",
        }:
            raise ValueError("Browser form-fill payload is invalid")
        if not isinstance(payload["text"], str) or len(payload["text"]) > 2000:
            raise ValueError("Browser form text exceeds the safety limit")
        session_id, session = self._session(payload)
        element, record = self._control(session, payload, "fields")
        if record["input_type"] not in {"", "text", "email", "search", "tel", "url", "number"}:
            raise ValueError("This browser field type is not permitted")
        if record["tag"] == "select":
            element.select_option(value=payload["text"], timeout=5000)
        else:
            element.fill(payload["text"], timeout=5000)
        matches = element.input_value(timeout=5000) == payload["text"]
        return {
            "success": matches,
            "message": "Approved form field filled" if matches else "Form field value could not be verified",
            "session_id": session_id,
            "data": {"value_matches": matches},
            "verification_status": "verified" if matches else "unverified",
            "page_identity": session["page_identity"],
            "navigation_generation": session["navigation_generation"],
        }

    def _expected_result_matches(self, page, expectation):
        if not isinstance(expectation, dict):
            return False
        if set(expectation) == {"kind", "url"} and expectation["kind"] == "url":
            actual_url, _ = self._resolve(page.url, self._allowed_domains())
            expected_url, _ = self._resolve(expectation["url"], self._allowed_domains())
            return actual_url == expected_url
        if set(expectation) == {"kind", "text"} and expectation["kind"] == "visible_text":
            text = expectation["text"]
            if not isinstance(text, str) or not text or len(text) > 500:
                return False
            return page.get_by_text(text, exact=False).first.is_visible(timeout=3000)
        return False

    def _submit(self, payload):
        if set(payload) != {
            "session_id", "element_id", "expected_result", "page_identity",
            "navigation_generation",
        }:
            raise ValueError("Browser submission payload is invalid")
        session_id, session = self._session(payload)
        element, record = self._control(session, payload, "buttons")
        if record["tag"] == "a":
            raise ValueError("Links are not accepted as form-submit controls")
        element.click(timeout=5000)
        try:
            session["page"].wait_for_load_state("domcontentloaded", timeout=5000)
        except _browser_imports()[2]:
            pass
        verified = self._expected_result_matches(session["page"], payload["expected_result"])
        return {
            "success": verified,
            "message": (
                "Approved browser submission completed and verified"
                if verified
                else "Submission may have occurred but could not be verified; do not retry"
            ),
            "session_id": session_id,
            "data": {"verification_status": "verified" if verified else "unverified"},
            "verification_status": "verified" if verified else "unverified",
            "page_identity": session["page_identity"],
            "navigation_generation": session["navigation_generation"],
        }

    def _download(self, payload):
        if set(payload) != {
            "session_id", "element_id", "page_identity",
            "navigation_generation",
        }:
            raise ValueError("Browser download payload is invalid")
        session_id, session = self._session(payload)
        _element, record = self._control(session, payload, "buttons")
        if record["tag"] != "a":
            raise ValueError("Only an observed download link is permitted")
        name, byte_count = self._bounded_https_download(
            record.get("href"),
            session["domains"],
        )
        return {
            "success": True,
            "message": "Approved download saved to the restricted directory",
            "session_id": session_id,
            "data": {"filename": name, "bytes": byte_count},
            "verification_status": "verified",
            "page_identity": session["page_identity"],
            "navigation_generation": session["navigation_generation"],
        }

    def _download_directory(self):
        directory = Path(
            os.getenv(
                "NEX_BROWSER_DOWNLOAD_DIR",
                str(Path.home() / ".local/share/nex-agent/downloads"),
            )
        ).expanduser()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = directory.resolve(strict=True)
        if directory.stat().st_mode & 0o077:
            raise ValueError("Browser download directory permissions are unsafe")
        return directory

    def _new_https_connection(self, hostname, address):
        if self._connection_factory is not None:
            return self._connection_factory(hostname, address)
        return _PinnedHTTPSConnection(hostname, address, timeout=10)

    def _bounded_https_download(self, url, domains):
        if not isinstance(url, str):
            raise ValueError("Download URL is invalid")
        normalized_url, _ = self._resolve(url, domains)
        current_url = normalized_url
        response = None
        connection = None
        temporary_path = None
        try:
            for redirect_count in range(6):
                normalized_url, addresses = self._resolve(current_url, domains)
                parsed = urlsplit(normalized_url)
                connection = self._new_https_connection(parsed.hostname, addresses[0])
                target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Host": parsed.hostname,
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                    },
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    response.close()
                    connection.close()
                    response = connection = None
                    if not location or redirect_count >= 5:
                        raise ValueError("Download redirect is invalid or excessive")
                    current_url = urljoin(normalized_url, location)
                    continue
                if response.status < 200 or response.status >= 300:
                    raise ValueError("Download server returned an error")

                content_length = response.getheader("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise ValueError("Download length is invalid") from exc
                    if declared_length < 0:
                        raise ValueError("Download length is invalid")
                    if declared_length > MAX_DOWNLOAD_BYTES:
                        raise ValueError("Download exceeds the safety limit")

                name = self._download_filename(
                    normalized_url,
                    response.getheader("Content-Disposition"),
                )
                directory = self._download_directory()
                destination = directory / name
                if destination.exists() or destination.is_symlink():
                    raise ValueError("Browser download destination already exists")

                byte_count = 0
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=".nex-download-",
                    dir=directory,
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        byte_count += len(chunk)
                        if byte_count > MAX_DOWNLOAD_BYTES:
                            raise ValueError("Download exceeds the safety limit")
                        temporary.write(chunk)
                if content_length is not None and byte_count != declared_length:
                    raise OSError("Download was interrupted")
                os.link(temporary_path, destination)
                temporary_path.unlink()
                temporary_path = None
                return name, byte_count
            raise ValueError("Download redirect is invalid")
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise OSError("Download was interrupted") from exc
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _download_filename(url, content_disposition):
        candidate = None
        if content_disposition:
            message = Message()
            message["content-disposition"] = content_disposition
            candidate = message.get_filename()
        if not candidate:
            candidate = unquote(Path(urlsplit(url).path).name)
        candidate = Path(candidate.replace("\\", "/")).name
        candidate = re.sub(r"[^A-Za-z0-9._ -]", "_", candidate).strip(" .")
        if not candidate or candidate in {".", ".."}:
            candidate = "download-" + uuid.uuid4().hex
        if len(candidate) > 180:
            candidate = candidate[-180:]
        return candidate

    def _close_one(self, session_id):
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session["context"].close()
            session["browser"].close()
            session["manager"].stop()

    def _close_session(self, payload):
        if set(payload) != {"session_id"}:
            raise ValueError("Browser close payload is invalid")
        session_id = _session_identifier(payload["session_id"])
        self._close_one(session_id)
        return {
            "success": True,
            "message": "Isolated browser session closed",
            "session_id": session_id,
            "verification_status": "verified",
        }

    def stop(self):
        with self._lock:
            for session_id in list(self._sessions):
                self._close_one(session_id)
