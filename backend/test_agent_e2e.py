import asyncio
import json
import os
import socket
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import main
import uvicorn
from local_agent_protocol import handle_command, parse_message
from websockets.asyncio.client import connect


class AgentEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = main.DATABASE
        main.DATABASE = str(Path(self.temp_dir.name) / "agent-e2e.db")
        main.init_db()
        main.AGENT_CONNECTIONS.clear()
        main.PENDING_AGENT_COMMANDS.clear()
        connection = main.get_db()
        try:
            connection.execute(
                """
                INSERT INTO customers (id, email, password_hash)
                VALUES ('e2e-customer', 'e2e@test.local', 'test-hash')
                """
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        main.AGENT_CONNECTIONS.clear()
        main.PENDING_AGENT_COMMANDS.clear()
        main.DATABASE = self.original_database
        self.temp_dir.cleanup()

    def test_customer_approval_websocket_agent_result_and_retrieval(self):
        asyncio.run(self.run_end_to_end())

    async def run_end_to_end(self):
        with patch.dict(os.environ, {
            "NEX_ENV": "development",
            "NEX_JWT_SECRET": "e" * 32,
        }):
            token = main.create_access_token("e2e-customer")
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.setblocking(False)
            host, port = listener.getsockname()
            server = uvicorn.Server(
                uvicorn.Config(
                    main.app,
                    log_level="critical",
                    access_log=False,
                    lifespan="off",
                )
            )
            server_task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                for _ in range(500):
                    if server.started:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(server.started, "Uvicorn test server did not start")

                def request(method, path, body=None):
                    headers = {"Authorization": f"Bearer {token}"}
                    data = None
                    if body is not None:
                        headers["Content-Type"] = "application/json"
                        data = json.dumps(body).encode("utf-8")
                    req = urllib.request.Request(
                        f"http://{host}:{port}{path}",
                        data=data,
                        headers=headers,
                        method=method,
                    )
                    with urllib.request.urlopen(req, timeout=10) as response:
                        return response.status, json.loads(response.read())

                status, paired = await asyncio.to_thread(
                    request,
                    "POST",
                    "/computer/agent/devices",
                    {"name": "E2E device", "permissions": ["open_app"]},
                )
                self.assertEqual(status, 201)
                device_id = paired["device"]["id"]
                websocket_url = (
                    f"ws://{host}:{port}/computer/agent/ws/{device_id}"
                )
                async with connect(websocket_url) as agent:
                    await agent.send(json.dumps({
                        "type": "authenticate",
                        "credential": paired["device_credential"],
                    }))
                    self.assertEqual(
                        parse_message(await agent.recv()),
                        {"type": "authenticated", "device_id": device_id},
                    )

                    status, requested = await asyncio.to_thread(
                        request,
                        "POST",
                        f"/computer/agent/devices/{device_id}/commands",
                        {
                            "action": "open_app",
                            "payload": {"app_id": "calculator"},
                            "client_request_id": "e2e-request-" + "x" * 24,
                        },
                    )
                    self.assertEqual(status, 202)
                    self.assertEqual(requested["status"], "pending_approval")

                    async def simulated_agent():
                        while True:
                            message = parse_message(await agent.recv())
                            if message.get("type") == "command":
                                result = handle_command(message)
                                await agent.send(json.dumps(result))
                            elif message.get("type") == "result_ack":
                                return message

                    agent_task = asyncio.create_task(simulated_agent())
                    approval_task = asyncio.create_task(asyncio.to_thread(
                        request,
                        "POST",
                        (
                            "/computer/agent/approvals/"
                            f"{requested['approval']['request_id']}/approve"
                        ),
                        {"approval_token": requested["approval"]["approval_token"]},
                    ))
                    ack = await asyncio.wait_for(agent_task, timeout=10)
                    self.assertTrue(ack["accepted"])
                    status, approved = await asyncio.wait_for(approval_task, timeout=10)
                    self.assertEqual(status, 200)
                    self.assertEqual(approved["status"], "failed")
                    self.assertFalse(approved["result"]["success"])
                    self.assertFalse(approved["execution_enabled"])

                    status, retrieved = await asyncio.to_thread(
                        request,
                        "GET",
                        f"/computer/agent/commands/{requested['command_id']}",
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(retrieved["status"], "failed")
                    self.assertEqual(
                        retrieved["result"]["command_id"],
                        requested["command_id"],
                    )
                    self.assertFalse(retrieved["result"]["success"])
            finally:
                server.should_exit = True
                await asyncio.wait_for(server_task, timeout=10)


if __name__ == "__main__":
    unittest.main()
