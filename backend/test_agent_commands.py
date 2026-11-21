import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from local_agent_protocol import handle_command, validate_command
from starlette.websockets import WebSocketDisconnect


CUSTOMER_A = {"id": "CUSTOMER-A"}
CUSTOMER_B = {"id": "CUSTOMER-B"}


class FakeAgentConnection:
    def __init__(self, device_id, reply=True, claimed_success=False):
        self.device_id = device_id
        self.reply = reply
        self.claimed_success = claimed_success
        self.sent = []

    async def send_json(self, command):
        self.sent.append(command)
        if self.reply:
            response = handle_command(command)
            response["success"] = self.claimed_success
            if self.claimed_success:
                response["message"] = "Desktop action completed"
            await main._record_agent_result(
                self.device_id,
                main.AgentCommandResult(**response),
            )

    async def close(self, **kwargs):
        return None


class FakeWebSocket:
    def __init__(self, messages, scheme="ws"):
        self.messages = list(messages)
        self.url = SimpleNamespace(scheme=scheme)
        self.headers = {}
        self.sent = []
        self.closed = []

    async def accept(self):
        return None

    async def receive_json(self):
        if self.messages:
            return self.messages.pop(0)
        raise WebSocketDisconnect(code=1000)

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, **kwargs):
        self.closed.append(kwargs)


class AgentCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = main.DATABASE
        main.DATABASE = str(Path(self.temp_dir.name) / "agent-test.db")
        main.init_db()
        main.AGENT_CONNECTIONS.clear()
        main.PENDING_AGENT_COMMANDS.clear()
        self.pairing = main.pair_agent_device(
            main.AgentDeviceCreate(name="Test device", permissions=["open_app", "capture_screen"]),
            CUSTOMER_A,
        )
        self.device = self.pairing["device"]

    def tearDown(self):
        main.AGENT_CONNECTIONS.clear()
        main.PENDING_AGENT_COMMANDS.clear()
        main.DATABASE = self.original_database
        self.temp_dir.cleanup()

    def connect_fake_agent(self, reply=True, claimed_success=False):
        connection = main.get_db()
        try:
            connection.execute(
                "UPDATE agent_devices SET status = 'connected' WHERE id = ?",
                (self.device["id"],),
            )
            connection.commit()
        finally:
            connection.close()
        fake = FakeAgentConnection(
            self.device["id"],
            reply=reply,
            claimed_success=claimed_success,
        )
        main.AGENT_CONNECTIONS[self.device["id"]] = fake
        return fake

    def request_command(self, customer=CUSTOMER_A, action="open_app", payload=None, request_id=None):
        return main.request_agent_command(
            self.device["id"],
            main.AgentCommandRequest(
                action=action,
                payload=payload if payload is not None else {"app_id": "calculator"},
                client_request_id=request_id or "request-" + "x" * 24,
            ),
            customer,
        )

    def test_approval_dispatches_validation_only_and_is_one_time(self):
        agent = self.connect_fake_agent()
        requested = self.request_command()
        self.assertEqual(requested["status"], "awaiting_approval")
        approval = requested["approval"]

        result = asyncio.run(
            main.approve_agent_command(
                approval["request_id"],
                main.AgentApprovalDecision(approval_token=approval["approval_token"]),
                CUSTOMER_A,
            )
        )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["result"]["success"])
        self.assertFalse(result["execution_enabled"])
        self.assertEqual(agent.sent[0]["execution_enabled"], False)
        self.assertEqual(agent.sent[0]["action"], "open_app")

        with self.assertRaises(Exception) as context:
            asyncio.run(
                main.approve_agent_command(
                    approval["request_id"],
                    main.AgentApprovalDecision(approval_token=approval["approval_token"]),
                    CUSTOMER_A,
                )
            )
        self.assertEqual(getattr(context.exception, "status_code", None), 404)

    def test_customer_isolation_and_request_replay_protection(self):
        self.connect_fake_agent()
        request_id = "request-" + "y" * 24
        with self.assertRaises(Exception) as context:
            self.request_command(customer=CUSTOMER_B, request_id=request_id)
        self.assertEqual(getattr(context.exception, "status_code", None), 404)

        first = self.request_command(request_id=request_id)
        with self.assertRaises(Exception) as context:
            self.request_command(request_id=request_id)
        self.assertEqual(getattr(context.exception, "status_code", None), 409)

        with self.assertRaises(Exception) as context:
            main.get_agent_command(first["command_id"], CUSTOMER_B)
        self.assertEqual(getattr(context.exception, "status_code", None), 404)

    def test_disconnected_device_and_unsafe_payload_fail_closed(self):
        with self.assertRaises(Exception) as context:
            self.request_command()
        self.assertEqual(getattr(context.exception, "status_code", None), 409)

        self.connect_fake_agent()
        with self.assertRaises(Exception) as context:
            self.request_command(payload={"app_id": ["calculator"]})
        self.assertEqual(getattr(context.exception, "status_code", None), 422)

    def test_emergency_stop_blocks_requests_and_local_agent_never_executes(self):
        self.connect_fake_agent()
        asyncio.run(main.emergency_stop(CUSTOMER_A))
        with self.assertRaises(Exception) as context:
            self.request_command()
        self.assertEqual(getattr(context.exception, "status_code", None), 423)

        command = {
            "type": "command",
            "command_id": "command-1",
            "action": "open_app",
            "payload": {"app_id": "calculator"},
            "execution_enabled": False,
        }
        result = handle_command(command)
        self.assertEqual(result["command_id"], "command-1")
        self.assertEqual(result["action"], "open_app")
        self.assertFalse(result["success"])
        self.assertIn("disabled", result["message"])

        with self.assertRaises(ValueError):
            validate_command({**command, "payload": {"app_id": "shell"}})
        with self.assertRaises(ValueError):
            validate_command({**command, "extra": True})

    def test_unsolicited_agent_result_is_rejected(self):
        self.connect_fake_agent()
        result = main.AgentCommandResult(
            command_id="not-a-pending-command",
            action="open_app",
            success=True,
            message="forged",
        )
        self.assertFalse(asyncio.run(main._record_agent_result(self.device["id"], result)))

    def test_agent_success_claim_cannot_enable_desktop_execution(self):
        self.connect_fake_agent(claimed_success=True)
        requested = self.request_command(
            action="capture_screen",
            payload={},
            request_id="request-" + "c" * 24,
        )
        result = asyncio.run(
            main.approve_agent_command(
                requested["approval"]["request_id"],
                main.AgentApprovalDecision(
                    approval_token=requested["approval"]["approval_token"]
                ),
                CUSTOMER_A,
            )
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["result"]["success"])
        self.assertFalse(main.get_agent_command(requested["command_id"], CUSTOMER_A)["result"]["success"])

    def test_denial_and_device_revocation_cancel_pending_work(self):
        self.connect_fake_agent()
        denied = self.request_command(request_id="request-" + "d" * 24)
        approval = denied["approval"]
        response = main.deny_agent_approval(
            approval["request_id"],
            main.AgentApprovalDecision(approval_token=approval["approval_token"]),
            CUSTOMER_A,
        )
        self.assertEqual(response["status"], "denied")

        pending = self.request_command(request_id="request-" + "r" * 24)
        asyncio.run(main.revoke_agent_device(self.device["id"], CUSTOMER_A))
        command = main.get_agent_command(pending["command_id"], CUSTOMER_A)
        self.assertEqual(command["status"], "cancelled")
        self.assertEqual(main.list_agent_devices(CUSTOMER_B)["devices"], [])

    def test_restart_fails_stale_dispatched_commands_closed(self):
        self.connect_fake_agent()
        requested = self.request_command(request_id="request-" + "s" * 24)
        connection = main.get_db()
        try:
            connection.execute(
                "UPDATE agent_commands SET status = 'dispatched' WHERE id = ?",
                (requested["command_id"],),
            )
            connection.commit()
        finally:
            connection.close()

        main.init_db()
        command = main.get_agent_command(requested["command_id"], CUSTOMER_A)
        self.assertEqual(command["status"], "failed")
        self.assertFalse(command["result"]["success"])
        self.assertIn("restarted", command["result"]["message"])

    def test_agent_websocket_requires_credential_and_secure_production_transport(self):
        secure_mode = {"NEX_ENV": "production"}
        invalid = FakeWebSocket([
            {"type": "authenticate", "credential": "wrong-credential"},
        ])
        with patch.dict(os.environ, secure_mode):
            asyncio.run(main.local_agent_websocket(invalid, self.device["id"]))
        self.assertTrue(invalid.closed)

        insecure = FakeWebSocket([], scheme="ws")
        with patch.dict(os.environ, secure_mode):
            asyncio.run(main.local_agent_websocket(insecure, self.device["id"]))
        self.assertEqual(insecure.closed[0]["reason"], "Secure WebSocket is required")

        valid = FakeWebSocket([
            {
                "type": "authenticate",
                "credential": self.pairing["device_credential"],
            },
        ])
        with patch.dict(os.environ, {"NEX_ENV": "development"}):
            asyncio.run(main.local_agent_websocket(valid, self.device["id"]))
        self.assertEqual(valid.sent[0]["type"], "authenticated")
        self.assertEqual(main.list_agent_devices(CUSTOMER_A)["devices"][0]["status"], "disconnected")

    def test_approval_expiration_and_response_timeout_are_recorded(self):
        agent = self.connect_fake_agent(reply=False)
        requested = self.request_command(request_id="request-" + "z" * 24)

        connection = main.get_db()
        try:
            connection.execute(
                "UPDATE agent_approvals SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (requested["approval"]["request_id"],),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(Exception) as context:
            asyncio.run(
                main.approve_agent_command(
                    requested["approval"]["request_id"],
                    main.AgentApprovalDecision(
                        approval_token=requested["approval"]["approval_token"]
                    ),
                    CUSTOMER_A,
                )
            )
        self.assertEqual(getattr(context.exception, "status_code", None), 409)

        # A separate request exercises the dispatch timeout without waiting for the default.
        timeout_request = main.request_agent_command(
            self.device["id"],
            main.AgentCommandRequest(
                action="open_app",
                payload={"app_id": "calculator"},
                client_request_id="request-" + "t" * 24,
            ),
            CUSTOMER_A,
        )
        original_timeout = main.AGENT_COMMAND_TIMEOUT_SECONDS
        main.AGENT_COMMAND_TIMEOUT_SECONDS = 0.01
        try:
            result = asyncio.run(
                main.approve_agent_command(
                    timeout_request["approval"]["request_id"],
                    main.AgentApprovalDecision(
                        approval_token=timeout_request["approval"]["approval_token"]
                    ),
                    CUSTOMER_A,
                )
            )
        finally:
            main.AGENT_COMMAND_TIMEOUT_SECONDS = original_timeout

        self.assertEqual(result["status"], "failed")
        self.assertIn("timed out", result["result"]["message"])
        self.assertEqual(len(agent.sent), 1)


if __name__ == "__main__":
    unittest.main()
