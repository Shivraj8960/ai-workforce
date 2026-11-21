"""Validation-only WebSocket client for a paired NEX Local Agent."""

import asyncio
import json
import os
from collections import OrderedDict
from urllib.parse import quote, urlsplit, urlunsplit

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
        }
    ):
        return None
    return {
        "command_id": command_id,
        "action": action,
        "success": False,
        "message": "Command rejected by Local Agent validation",
    }


async def _run_connection(url, device_id, credential, seen_commands, runtime=None):
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
                    result = handle_command(message, runtime=runtime)
                except ValueError:
                    result = _rejection_result(message)
                    if result is None:
                        raise
                if isinstance(command_id, str) and command_id:
                    seen_commands[command_id] = None
                    seen_commands.move_to_end(command_id)
                    while len(seen_commands) > MAX_SEEN_COMMANDS:
                        seen_commands.popitem(last=False)

                async with send_lock:
                    await websocket.send(json.dumps(result, separators=(",", ":")))
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            if runtime is not None:
                runtime.stop()


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
            await _run_connection(url, device_id, credential, seen_commands, runtime=runtime)
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
