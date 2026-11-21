import sqlite3
import unittest

from migrations import (
    _add_rate_limit_scopes,
    _bind_approval_hash_to_device,
    _add_computer_use_registry,
    _create_phase1_tables,
    _expand_desktop_actions_and_add_registries,
    _index_rate_event_expiry,
    _rebuild_with_constraints,
    apply_migrations,
    payload_hash,
)


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

    def test_phase6_preserves_commands_and_adds_desktop_registries(self):
        self.insert_device()
        self.connection.execute(
            """
            INSERT INTO agent_commands
            (id, owner_id, device_id, client_request_id, action, payload,
             status, created_at, updated_at, deadline_at)
            VALUES ('command-a', 'owner-a', 'device-a', 'request-a',
                    'open_app', '{"app_id":"calculator"}', 'failed',
                    '2025-01-01', '2025-01-01', '2025-01-02')
            """
        )
        self.connection.commit()

        apply_migrations(self.connection)

        row = self.connection.execute(
            "SELECT action, status FROM agent_commands WHERE id = 'command-a'"
        ).fetchone()
        self.assertEqual(row["action"], "open_app")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM agent_desktop_capabilities"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM agent_execution_sessions"
            ).fetchone()[0],
            0,
        )
        self.assertIn(
            7,
            {
                row["version"]
                for row in self.connection.execute(
                    "SELECT version FROM schema_migrations"
                )
            },
        )
        proposal_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(computer_action_proposals)"
            )
        }
        self.assertTrue(
            {"instruction", "payload_hash", "verification_status"} <= proposal_columns
        )

        self.connection.execute(
            """
            INSERT INTO agent_commands
            (id, owner_id, device_id, client_request_id, action, payload,
             status, created_at, updated_at, deadline_at)
            VALUES ('command-b', 'owner-a', 'device-a', 'request-b',
                    'get_active_window', '{}', 'pending_approval',
                    '2025-01-01', '2025-01-01', '2025-01-02')
            """
        )
        self.connection.execute(
            """
            INSERT INTO agent_commands
            (id, owner_id, device_id, client_request_id, action, payload,
             status, created_at, updated_at, deadline_at)
            VALUES ('command-browser', 'owner-a', 'device-a', 'request-browser',
                    'browser_navigate', '{"session_id":"12345678-1234-1234-1234-123456789abc","url":"https://example.com/"}',
                    'pending_approval', '2025-01-01', '2025-01-01', '2025-01-02')
            """
        )
        self.connection.commit()

    def test_phase7_preserves_populated_records_foreign_keys_and_legacy_indexes(self):
        migrations = (
            (2, _rebuild_with_constraints),
            (3, _add_rate_limit_scopes),
            (4, _bind_approval_hash_to_device),
            (5, _index_rate_event_expiry),
            (6, _expand_desktop_actions_and_add_registries),
        )
        for version, migration in migrations:
            with self.connection:
                migration(self.connection)
                self.connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (version,),
                )

        self.insert_device(status="paired")
        self.connection.execute(
            """
            INSERT INTO agent_commands
            (id, owner_id, device_id, client_request_id, action, payload,
             status, result, created_at, updated_at, deadline_at)
            VALUES ('preserved-command', 'owner-a', 'device-a', 'request-preserved',
                    'open_app', '{"app_id":"calculator"}', 'completed',
                    '{"success":true}', '2025-01-01', '2025-01-01', '2025-01-02')
            """
        )
        self.connection.execute(
            """
            INSERT INTO agent_approvals
            (id, owner_id, command_id, token_hash, payload_hash, status,
             expires_at, created_at, consumed_at)
            VALUES ('preserved-approval', 'owner-a', 'preserved-command',
                    'opaque-token-hash', 'opaque-payload-hash', 'approved',
                    '2025-01-02', '2025-01-01', '2025-01-01')
            """
        )
        self.connection.execute(
            """
            INSERT INTO agent_audit_log
            (id, owner_id, device_id, command_id, event, details, created_at)
            VALUES (73, 'owner-a', 'device-a', 'preserved-command',
                    'preserved-event', '{"safe":true}', '2025-01-01')
            """
        )
        self.connection.execute(
            """
            INSERT INTO agent_execution_sessions
            (id, owner_id, device_id, command_id, adapter, state, started_at, ended_at)
            VALUES ('preserved-session', 'owner-a', 'device-a', 'preserved-command',
                    'desktop', 'completed', '2025-01-01', '2025-01-01')
            """
        )
        self.connection.commit()

        indexes = {
            "idx_agent_commands_owner": ("owner_id", "created_at"),
            "idx_agent_approvals_owner": ("owner_id", "status", "expires_at"),
            "idx_agent_audit_owner": ("owner_id", "created_at"),
            "idx_agent_execution_owner": ("owner_id", "started_at"),
        }
        before = {
            row["name"]
            for row in self.connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'index' AND name IN (
                    'idx_agent_commands_owner', 'idx_agent_approvals_owner',
                    'idx_agent_audit_owner', 'idx_agent_execution_owner'
                )
                """
            )
        }
        self.assertEqual(before, set(indexes))

        apply_migrations(self.connection)

        for name, columns in indexes.items():
            index_columns = tuple(
                row["name"]
                for row in self.connection.execute(f"PRAGMA index_info({name})")
            )
            self.assertEqual(index_columns, columns)
            self.assertIsNotNone(
                self.connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                    (name,),
                ).fetchone()
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT result FROM agent_commands WHERE id = 'preserved-command'"
            ).fetchone()["result"],
            '{"success":true}',
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT token_hash FROM agent_approvals WHERE id = 'preserved-approval'"
            ).fetchone()["token_hash"],
            "opaque-token-hash",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT id FROM agent_audit_log WHERE id = 73"
            ).fetchone()["id"],
            73,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT command_id FROM agent_execution_sessions WHERE id = 'preserved-session'"
            ).fetchone()["command_id"],
            "preserved-command",
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_phase8_adds_click_binding_without_replacing_existing_observations(self):
        migrations = (
            (2, _rebuild_with_constraints),
            (3, _add_rate_limit_scopes),
            (4, _bind_approval_hash_to_device),
            (5, _index_rate_event_expiry),
            (6, _expand_desktop_actions_and_add_registries),
            (7, _add_computer_use_registry),
        )
        for version, migration in migrations:
            with self.connection:
                migration(self.connection)
                self.connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (version,),
                )
        self.insert_device()
        self.connection.execute(
            """
            INSERT INTO computer_observations
            (id, owner_id, device_id, source, screen_width, screen_height,
             provider, image_sha256, elements, captured_at, expires_at)
            VALUES ('observation-existing', 'owner-a', 'device-a', 'screen',
                    800, 600, 'mock', 'a', '[]', '2026-01-01', '2027-01-01')
            """
        )
        self.connection.commit()

        apply_migrations(self.connection)

        row = self.connection.execute(
            """
            SELECT id, image_sha256, active_window_identity
            FROM computer_observations WHERE id = 'observation-existing'
            """
        ).fetchone()
        self.assertEqual(row["id"], "observation-existing")
        self.assertEqual(row["image_sha256"], "a")
        self.assertIsNone(row["active_window_identity"])
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

if __name__ == "__main__":
    unittest.main()
