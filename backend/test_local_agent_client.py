import asyncio
import json
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import local_agent_client


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        return json.dumps({"type": "authenticated", "device_id": "device-a"})

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.messages)
        except StopIteration:
            raise StopAsyncIteration


class LocalAgentClientTests(unittest.TestCase):
    def test_backend_url_uses_secure_transport_and_never_embeds_credentials(self):
        self.assertEqual(
            local_agent_client.build_agent_url("https://example.test/base", "device/a"),
            "wss://example.test/base/computer/agent/ws/device%2Fa",
        )
        self.assertEqual(
            local_agent_client.build_agent_url(
                "http://localhost:8000", "device-a", development=True
            ),
            "ws://localhost:8000/computer/agent/ws/device-a",
        )
        with self.assertRaises(ValueError):
            local_agent_client.build_agent_url(
                "http://example.test", "device-a"
            )
        with self.assertRaises(ValueError):
            local_agent_client.build_agent_url(
                "https://user:secret@example.test", "device-a"
            )

    def test_local_agent_validates_and_deduplicates_without_desktop_execution(self):
        command = {
            "type": "command",
            "command_id": "command-a",
            "action": "open_app",
            "payload": {"app_id": "calculator"},
            "execution_enabled": False,
        }
        fake = FakeWebSocket([
            json.dumps(command),
            json.dumps(command),
        ])
        with patch.object(local_agent_client, "connect", return_value=fake):
            asyncio.run(
                local_agent_client._run_connection(
                    "wss://example.test/ws",
                    "device-a",
                    "paired-credential",
                    OrderedDict(),
                )
            )

        self.assertEqual(fake.sent[0], {
            "type": "authenticate",
            "credential": "paired-credential",
        })
        results = [message for message in fake.sent if "command_id" in message]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["command_id"], "command-a")
        self.assertEqual(results[0]["action"], "open_app")
        self.assertFalse(results[0]["success"])
        self.assertIn("disabled", results[0]["message"])

    def test_local_agent_reconnects_after_transient_disconnect(self):
        async def run():
            with patch.object(
                local_agent_client,
                "_run_connection",
                new=AsyncMock(
                    side_effect=[
                        OSError("temporary disconnect"),
                        local_agent_client.AgentAuthenticationError("revoked"),
                    ]
                ),
            ) as connection, patch.object(
                local_agent_client,
                "asyncio",
                SimpleNamespace(sleep=AsyncMock()),
            ) as asyncio_module:
                with self.assertRaises(local_agent_client.AgentAuthenticationError):
                    await local_agent_client.run_agent(
                        "https://example.test",
                        "device-a",
                        "paired-credential",
                    )
                self.assertEqual(connection.await_count, 2)
                asyncio_module.sleep.assert_awaited_once_with(1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
