"""Validation-only WebSocket client for a paired NEX Local Agent."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
import json
import os
from collections import OrderedDict
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from websockets.asyncio.client import connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
)

from local_agent_protocol import MAX_MESSAGE_BYTES, handle_command, parse_message


HEARTBEAT_SECONDS = 15
MAX_RECONNECT_SECONDS = 30
MAX_SEEN_COMMANDS = 10000


class AgentAuthenticationError(RuntimeError):
    pass


def build_agent_url(backend_url, device_id, development=False):
    parsed = urlsplit(backend_url)
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Backend URL is invalid")
    if parsed.scheme in {"https", "wss"}:
        scheme = "wss"
    elif development and parsed.scheme in {"http", "ws"}:
        scheme = "ws"
    else:
        raise ValueError("Local Agent requires a secure WSS backend URL")
    path = parsed.path.rstrip("/") + "/computer/agent/ws/" + quote(device_id, safe="")
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def build_observation_url(backend_url, device_id, command_id, development=False):
    parsed = urlsplit(backend_url)
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Backend URL is invalid")
    if parsed.scheme in {"https", "wss"}:
        scheme = "https"
    elif development and parsed.scheme in {"http", "ws"}:
        scheme = "http"
    else:
        raise ValueError("Local Agent requires a secure HTTPS backend URL")
    path = (
        parsed.path.rstrip("/")
        + "/computer/agent/devices/"
        + quote(device_id, safe="")
        + "/commands/"
        + quote(command_id, safe="")
        + "/observation"
    )
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _upload_observation(
    backend_url, device_id, command_id, credential, image_bytes, development,
    active_window_identity=None,
):
    if (
        not isinstance(image_bytes, bytes)
        or not image_bytes
        or len(image_bytes) > 5 * 1024 * 1024
    ):
        raise ValueError("Captured screenshot is invalid or too large")
    request = Request(
        build_observation_url(
            backend_url, device_id, command_id, development=development
        ),
        data=image_bytes,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Device-Credential": credential,
            **(
                {"X-Active-Window-Identity": active_window_identity}
                if isinstance(active_window_identity, str)
                else {}
            ),
        },
        method="POST",
    )
    with urlopen(request, timeout=25) as response:
        body = response.read(8193)
        if response.status != 201 or len(body) > 8192:
            raise RuntimeError("Backend rejected the screen observation")
    result = parse_message(body.decode("utf-8"))
    observation_id = result.get("observation_id")
    if result.get("status") != "created" or not isinstance(observation_id, str):
        raise RuntimeError("Backend returned an invalid observation response")
    return observation_id


async def _send_heartbeats(websocket, send_lock):
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        async with send_lock:
            await websocket.send(json.dumps({"type": "heartbeat"}))


def _rejection_result(command):
    command_id = command.get("command_id")
    action = command.get("action")
    if (
        not isinstance(command_id, str)
        or not command_id
        or len(command_id) > 64
        or not isinstance(action, str)
        or action not in {
            "open_app",
            "capture_screen",
            "get_active_window",
            "input_text",
            "read_file",
            "write_file",
            "click",
            "browser_navigate",
            "browser_read_page",
            "browser_inspect_controls",
            "browser_fill",
            "browser_submit",
            "browser_download",
            "browser_close",
        }
    ):
        return None
    return {
        "command_id": command_id,
        "action": action,
        "success": False,
        "message": "Command rejected by Local Agent validation",
    }


async def _run_connection(
    url,
    device_id,
    credential,
    seen_commands,
    runtime=None,
    backend_url=None,
    development=False,
):
    async with connect(
        url,
        open_timeout=10,
        close_timeout=5,
        max_size=MAX_MESSAGE_BYTES,
        ping_interval=HEARTBEAT_SECONDS,
        ping_timeout=HEARTBEAT_SECONDS,
    ) as websocket:
        await websocket.send(json.dumps({
            "type": "authenticate",
            "credential": credential,
        }))
        try:
            auth_response = parse_message(await websocket.recv())
        except ValueError as exc:
            raise AgentAuthenticationError("Backend authentication response is invalid") from exc
        if auth_response != {"type": "authenticated", "device_id": device_id}:
            raise AgentAuthenticationError("Backend rejected Local Agent authentication")

        if runtime is not None:
            capabilities = runtime.capabilities()
            session_type = os.getenv("XDG_SESSION_TYPE", "unknown").strip().lower() or "unknown"
            await websocket.send(json.dumps({
                "type": "capabilities",
                "capabilities": capabilities,
                "session_type": session_type,
            }, separators=(",", ":")))

        send_lock = asyncio.Lock()
        command_executor = ThreadPoolExecutor(max_workers=1)
        heartbeat = asyncio.create_task(_send_heartbeats(websocket, send_lock))
        try:
            async for raw_message in websocket:
                message = parse_message(raw_message)
                message_type = message.get("type")
                if message_type in {"heartbeat_ack", "result_ack", "capabilities_ack"}:
                    continue
                if message_type != "command":
                    raise ValueError("Unexpected backend WebSocket message")

                command_id = message.get("command_id")
                if isinstance(command_id, str) and command_id in seen_commands:
                    continue
                try:
                    result = await asyncio.get_running_loop().run_in_executor(
                        command_executor,
                        functools.partial(handle_command, message, runtime=runtime),
                    )
                except ValueError:
                    result = _rejection_result(message)
                    if result is None:
                        raise
                image_bytes = result.pop("image_bytes", None)
                active_window_identity = result.pop("active_window_identity", None)
                if image_bytes is not None:
                    try:
                        observation_id = await asyncio.get_running_loop().run_in_executor(
                            command_executor,
                            functools.partial(
                                _upload_observation,
                                backend_url
                                or url.rsplit("/computer/agent/ws/", 1)[0],
                                device_id,
                                command_id,
                                credential,
                                image_bytes,
                                development,
                                active_window_identity,
                            ),
                        )
                    except (OSError, RuntimeError, ValueError):
                        result["success"] = False
                        result["message"] = (
                            "Screenshot analysis failed; no observation was stored"
                        )
                    else:
                        result["observation_id"] = observation_id
                if isinstance(command_id, str) and command_id:
                    seen_commands[command_id] = None
                    seen_commands.move_to_end(command_id)
                    while len(seen_commands) > MAX_SEEN_COMMANDS:
                        seen_commands.popitem(last=False)

                async with send_lock:
                    encoded_result = json.dumps(result, separators=(",", ":"))
                    if len(encoded_result.encode("utf-8")) > MAX_MESSAGE_BYTES:
                        raise ValueError("Local Agent result exceeds the protocol safety limit")
                    await websocket.send(encoded_result)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            if runtime is not None:
                await asyncio.get_running_loop().run_in_executor(
                    command_executor, runtime.stop
                )
            command_executor.shutdown(wait=True, cancel_futures=True)


async def run_agent(backend_url, device_id, credential, development=False):
    url = build_agent_url(backend_url, device_id, development=development)
    seen_commands = OrderedDict()
    runtime = None
    try:
        from desktop_runtime import DesktopRuntime
        runtime = DesktopRuntime()
    except ImportError:
        pass
    retry_delay = 1
    while True:
        try:
            if runtime is not None:
                runtime.reset()
            await _run_connection(
                url,
                device_id,
                credential,
                seen_commands,
                runtime=runtime,
                backend_url=backend_url,
                development=development,
            )
            retry_delay = 1
        except ConnectionClosed as exc:
            if exc.code == 1008:
                raise AgentAuthenticationError(
                    "Local Agent connection was rejected; check pairing or revocation"
                ) from None
        except AgentAuthenticationError:
            raise
        except (OSError, TimeoutError, ValueError, InvalidHandshake):
            pass

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, MAX_RECONNECT_SECONDS)


async def run_from_environment():
    backend_url = os.getenv("NEX_AGENT_BACKEND_URL", "").strip()
    device_id = os.getenv("NEX_AGENT_DEVICE_ID", "").strip()
    credential = os.getenv("NEX_AGENT_CREDENTIAL", "")
    if not backend_url or not device_id or not credential:
        raise RuntimeError(
            "NEX_AGENT_BACKEND_URL, NEX_AGENT_DEVICE_ID, and "
            "NEX_AGENT_CREDENTIAL are required"
        )
    mode = os.getenv("NEX_ENV", "production").strip().lower()
    await run_agent(
        backend_url,
        device_id,
        credential,
        development=(mode == "development"),
    )


if __name__ == "__main__":
    asyncio.run(run_from_environment())
