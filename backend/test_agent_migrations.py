import sqlite3
import unittest

from migrations import _create_phase1_tables, apply_migrations, payload_hash


class AgentMigrationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute(
            "CREATE TABLE customers (id TEXT PRIMARY KEY, email TEXT, password_hash TEXT)"
        )
        self.connection.execute(
            "INSERT INTO customers VALUES ('owner-a', 'a@test.local', 'hash')"
        )
        _create_phase1_tables(self.connection)
        self.connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        self.connection.execute(
            "INSERT INTO schema_migrations (version) VALUES (1)"
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def insert_device(self, owner_id="owner-a", status="connected"):
        self.connection.execute(
            """
            INSERT INTO agent_devices
            (id, owner_id, name, credential_hash, permissions, status)
            VALUES ('device-a', ?, 'Test device', 'credential-hash', '["open_app"]', ?)
            """,
            (owner_id, status),
        )

    def test_failed_migration_preserves_existing_data_and_records_safe_states(self):
        self.insert_device()
        self.connection.execute(
            """
            INSERT INTO agent_commands
            VALUES ('command-running', 'owner-a', 'device-a', 'request-running',
                    'open_app', '{"app_id":"calculator"}', 'dispatched', NULL,
                    '2025-01-01', '2025-01-01', '2025-01-02')
            """
        )
        self.connection.execute(
            """
            INSERT INTO agent_commands
            VALUES ('command-pending', 'owner-a', 'device-a', 'request-pending',
                    'open_app', '{"app_id":"calculator"}', 'awaiting_approval', NULL,
                    '2025-01-01', '2025-01-01', '2025-01-02')
            """
        )
        self.connection.execute(
            """
            INSERT INTO agent_approvals
            VALUES ('approval-a', 'owner-a', 'command-pending', 'token-hash',
                    'pending', '2025-01-02', '2025-01-01', NULL)
            """
        )
        self.connection.execute(
            """
            INSERT INTO agent_audit_log
            (owner_id, device_id, command_id, event, details, created_at)
            VALUES ('owner-a', 'device-a', 'command-pending',
                    'command_requested', '{}', '2025-01-01')
            """
        )
        self.connection.execute(
            "INSERT INTO agent_rate_events (owner_id, created_at) VALUES ('owner-a', '2025-01-01')"
        )
        self.connection.commit()

        apply_migrations(self.connection)

        device = self.connection.execute(
            "SELECT status, credential_hash FROM agent_devices WHERE id = 'device-a'"
        ).fetchone()
        self.assertEqual(device["status"], "disconnected")
        self.assertEqual(device["credential_hash"], "credential-hash")

        running = self.connection.execute(
            "SELECT status, result FROM agent_commands WHERE id = 'command-running'"
        ).fetchone()
        self.assertEqual(running["status"], "failed")
        self.assertIn("not resumed", running["result"])

        pending = self.connection.execute(
            "SELECT status FROM agent_commands WHERE id = 'command-pending'"
        ).fetchone()
        self.assertEqual(pending["status"], "pending_approval")

        approval = self.connection.execute(
            "SELECT payload_hash, token_hash FROM agent_approvals WHERE id = 'approval-a'"
        ).fetchone()
        self.assertEqual(
            approval["payload_hash"],
            payload_hash(
                "open_app",
                '{"app_id":"calculator"}',
                "device-a",
            ),
        )
        self.assertEqual(approval["token_hash"], "token-hash")

        events = {
            row["event"]
            for row in self.connection.execute(
                "SELECT event FROM agent_audit_log"
            )
        }
        self.assertIn("command_requested", events)
        self.assertIn("command_failed", events)
        self.assertIn("agent_disconnected", events)
        rate = self.connection.execute(
            "SELECT scope FROM agent_rate_events"
        ).fetchone()
        self.assertEqual(rate["scope"], "commands")
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_key_check").fetchall(),
            [],
        )

    def test_device_binding_migration_recomputes_legacy_approval_hashes(self):
        self.insert_device()
        self.connection.execute(
            """
            INSERT INTO agent_commands
            VALUES ('command-pending', 'owner-a', 'device-a', 'request-pending',
                    'open_app', '{"app_id":"calculator"}', 'awaiting_approval', NULL,
                    '2025-01-01', '2025-01-01', '2025-01-02')
            """
        )
        self.connection.execute(
            """
            INSERT INTO agent_approvals
            VALUES ('approval-a', 'owner-a', 'command-pending', 'token-hash',
                    'pending', '2025-01-02', '2025-01-01', NULL)
            """
        )
        self.connection.commit()
        apply_migrations(self.connection)
        self.connection.execute(
            "UPDATE agent_approvals SET payload_hash = 'legacy-hash' WHERE id = 'approval-a'"
        )
        self.connection.execute(
            "DELETE FROM schema_migrations WHERE version = 4"
        )
        self.connection.commit()

        apply_migrations(self.connection)

        approval = self.connection.execute(
            "SELECT payload_hash FROM agent_approvals WHERE id = 'approval-a'"
        ).fetchone()
        self.assertEqual(
            approval["payload_hash"],
            payload_hash("open_app", '{"app_id":"calculator"}', "device-a"),
        )

    def test_orphaned_legacy_rows_abort_without_dropping_data(self):
        self.insert_device(owner_id="missing-owner", status="paired")
        self.connection.commit()

        with self.assertRaisesRegex(RuntimeError, "invalid ownership references"):
            apply_migrations(self.connection)

        row = self.connection.execute(
            "SELECT owner_id FROM agent_devices WHERE id = 'device-a'"
        ).fetchone()
        self.assertEqual(row["owner_id"], "missing-owner")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 2"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
