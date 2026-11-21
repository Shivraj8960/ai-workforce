"""Versioned, additive SQLite migrations for the Local Agent workflow."""

import hashlib
import json


_AGENT_TABLES = (
    "agent_approvals",
    "agent_commands",
    "agent_audit_log",
    "agent_rate_events",
    "agent_safety",
    "agent_devices",
)


def _create_phase1_tables(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_devices (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            name TEXT NOT NULL,
            credential_hash TEXT NOT NULL,
            permissions TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'paired',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_safety (
            owner_id TEXT PRIMARY KEY,
            emergency_stopped INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_commands (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            client_request_id TEXT NOT NULL,
            action TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            result TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deadline_at TEXT NOT NULL,
            UNIQUE(owner_id, client_request_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_approvals (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            consumed_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL,
            device_id TEXT,
            command_id TEXT,
            event TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_rate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _create_constrained_tables(connection):
    connection.execute(
        """
        CREATE TABLE agent_devices (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
            name TEXT NOT NULL,
            credential_hash TEXT NOT NULL,
            permissions TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'paired'
                CHECK(status IN ('paired', 'connected', 'disconnected', 'revoked')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT,
            UNIQUE(id, owner_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE agent_safety (
            owner_id TEXT PRIMARY KEY REFERENCES customers(id) ON DELETE RESTRICT,
            emergency_stopped INTEGER NOT NULL DEFAULT 0
                CHECK(emergency_stopped IN (0, 1)),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE agent_commands (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
            device_id TEXT NOT NULL,
            client_request_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('open_app', 'capture_screen')),
            payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'pending_approval', 'approved', 'denied', 'queued', 'executing',
                'completed', 'failed', 'expired', 'cancelled'
            )),
            result TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deadline_at TEXT NOT NULL,
            UNIQUE(owner_id, client_request_id),
            UNIQUE(id, owner_id),
            FOREIGN KEY(device_id, owner_id)
                REFERENCES agent_devices(id, owner_id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE agent_approvals (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
            command_id TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'approved', 'denied', 'expired', 'cancelled')),
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            consumed_at TEXT,
            FOREIGN KEY(command_id, owner_id)
                REFERENCES agent_commands(id, owner_id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE agent_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
            device_id TEXT REFERENCES agent_devices(id) ON DELETE SET NULL,
            command_id TEXT REFERENCES agent_commands(id) ON DELETE SET NULL,
            event TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE agent_rate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
            scope TEXT NOT NULL DEFAULT 'commands',
            created_at TEXT NOT NULL
        )
        """
    )


def payload_hash(action, payload, device_id, proposal_id=None, instruction=None):
    payload_value = json.loads(payload) if isinstance(payload, str) else payload
    fields = {
        "action": action,
        "device_id": device_id,
        "payload": payload_value,
    }
    if proposal_id is not None:
        fields["proposal_id"] = proposal_id
    if instruction is not None:
        fields["instruction"] = instruction
    canonical_payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _rebuild_with_constraints(connection):
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP INDEX IF EXISTS idx_agent_commands_owner")
        connection.execute("DROP INDEX IF EXISTS idx_agent_approvals_owner")
        connection.execute("DROP INDEX IF EXISTS idx_agent_audit_owner")
        for table in _AGENT_TABLES:
            connection.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")

        _create_constrained_tables(connection)

        connection.execute(
            """
            INSERT INTO agent_devices
            (id, owner_id, name, credential_hash, permissions, status,
             created_at, last_seen_at)
            SELECT id, owner_id, name, credential_hash, permissions,
                   CASE WHEN status = 'connected' THEN 'disconnected' ELSE status END,
                   created_at, last_seen_at
            FROM agent_devices_legacy
            """
        )
        connection.execute(
            """
            INSERT INTO agent_safety
            SELECT owner_id, emergency_stopped, updated_at
            FROM agent_safety_legacy
            """
        )

        command_rows = connection.execute(
            "SELECT * FROM agent_commands_legacy"
        ).fetchall()
        for row in command_rows:
            status = {
                "awaiting_approval": "pending_approval",
                "dispatched": "failed",
            }.get(row["status"], row["status"])
            result = row["result"]
            if row["status"] == "dispatched":
                result = json.dumps({
                    "command_id": row["id"],
                    "action": row["action"],
                    "success": False,
                    "message": "Command was not resumed during schema migration",
                })
            connection.execute(
                """
                INSERT INTO agent_commands
                (id, owner_id, device_id, client_request_id, action, payload,
                 status, result, created_at, updated_at, deadline_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"], row["owner_id"], row["device_id"],
                    row["client_request_id"], row["action"], row["payload"],
                    status, result, row["created_at"], row["updated_at"],
                    row["deadline_at"],
                ),
            )

        approval_rows = connection.execute(
            "SELECT * FROM agent_approvals_legacy"
        ).fetchall()
        for row in approval_rows:
            command = connection.execute(
                "SELECT action, payload, device_id FROM agent_commands WHERE id = ?",
                (row["command_id"],),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO agent_approvals
                (id, owner_id, command_id, token_hash, payload_hash, status,
                 expires_at, created_at, consumed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"], row["owner_id"], row["command_id"],
                    row["token_hash"],
                    payload_hash(
                        command["action"],
                        command["payload"],
                        command["device_id"],
                    ),
                    row["status"], row["expires_at"], row["created_at"],
                    row["consumed_at"],
                ),
            )

        connection.execute(
            """
            INSERT INTO agent_audit_log
            SELECT id, owner_id, device_id, command_id, event, details, created_at
            FROM agent_audit_log_legacy
            """
        )
        connection.execute(
            """
            INSERT INTO agent_audit_log
            (owner_id, device_id, event, details, created_at)
            SELECT owner_id, id, 'agent_disconnected',
                   '{"reason":"connection not resumed during schema migration"}',
                   CURRENT_TIMESTAMP
            FROM agent_devices_legacy
            WHERE status = 'connected'
            """
        )
        connection.execute(
            """
            INSERT INTO agent_audit_log
            (owner_id, device_id, command_id, event, details, created_at)
            SELECT owner_id, device_id, id, 'command_failed',
                   '{"reason":"command not resumed during schema migration"}',
                   CURRENT_TIMESTAMP
            FROM agent_commands_legacy
            WHERE status = 'dispatched'
            """
        )
        connection.execute(
            """
            INSERT INTO agent_rate_events
            (id, owner_id, scope, created_at)
            SELECT id, owner_id, 'commands', created_at FROM agent_rate_events_legacy
            """
        )
        for table in _AGENT_TABLES:
            connection.execute(f"DROP TABLE {table}_legacy")

        connection.execute(
            "CREATE INDEX idx_agent_commands_owner ON agent_commands(owner_id, created_at)"
        )
        connection.execute(
            "CREATE INDEX idx_agent_approvals_owner ON agent_approvals(owner_id, status, expires_at)"
        )
        connection.execute(
            "CREATE INDEX idx_agent_audit_owner ON agent_audit_log(owner_id, created_at)"
        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Agent database migration found invalid ownership references")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _add_rate_limit_scopes(connection):
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(agent_rate_events)")
    }
    if "scope" not in columns:
        connection.execute(
            "ALTER TABLE agent_rate_events ADD COLUMN scope TEXT NOT NULL DEFAULT 'commands'"
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_rate_scope
        ON agent_rate_events(owner_id, scope, created_at)
        """
    )


def _bind_approval_hash_to_device(connection):
    rows = connection.execute(
        """
        SELECT a.id, c.action, c.payload, c.device_id
        FROM agent_approvals a
        JOIN agent_commands c ON c.id = a.command_id AND c.owner_id = a.owner_id
        """
    ).fetchall()
    for row in rows:
        connection.execute(
            "UPDATE agent_approvals SET payload_hash = ? WHERE id = ?",
            (
                payload_hash(row["action"], row["payload"], row["device_id"]),
                row["id"],
            ),
        )


def _index_rate_event_expiry(connection):
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_rate_created
        ON agent_rate_events(created_at)
        """
    )


def _expand_desktop_actions_and_add_registries(connection):
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "ALTER TABLE agent_commands RENAME TO agent_commands_phase6_legacy"
        )
        connection.execute(
            "ALTER TABLE agent_approvals RENAME TO agent_approvals_phase6_legacy"
        )
        connection.execute(
            "ALTER TABLE agent_audit_log RENAME TO agent_audit_log_phase6_legacy"
        )

        connection.execute(
            """
            CREATE TABLE agent_commands (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                device_id TEXT NOT NULL,
                client_request_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN (
                    'open_app', 'capture_screen', 'get_active_window', 'input_text',
                    'read_file', 'write_file'
                )),
                payload TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'pending_approval', 'approved', 'denied', 'queued', 'executing',
                    'completed', 'failed', 'expired', 'cancelled'
                )),
                result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                UNIQUE(owner_id, client_request_id),
                UNIQUE(id, owner_id),
                FOREIGN KEY(device_id, owner_id)
                    REFERENCES agent_devices(id, owner_id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_commands
            (id, owner_id, device_id, client_request_id, action, payload, status,
             result, created_at, updated_at, deadline_at)
            SELECT id, owner_id, device_id, client_request_id, action, payload, status,
                   result, created_at, updated_at, deadline_at
            FROM agent_commands_phase6_legacy
            """
        )

        connection.execute(
            """
            CREATE TABLE agent_approvals (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                command_id TEXT NOT NULL UNIQUE,
                token_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'denied', 'expired', 'cancelled')),
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                consumed_at TEXT,
                FOREIGN KEY(command_id, owner_id)
                    REFERENCES agent_commands(id, owner_id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_approvals
            (id, owner_id, command_id, token_hash, payload_hash, status,
             expires_at, created_at, consumed_at)
            SELECT id, owner_id, command_id, token_hash, payload_hash, status,
                   expires_at, created_at, consumed_at
            FROM agent_approvals_phase6_legacy
            """
        )

        connection.execute(
            """
            CREATE TABLE agent_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                device_id TEXT REFERENCES agent_devices(id) ON DELETE SET NULL,
                command_id TEXT REFERENCES agent_commands(id) ON DELETE SET NULL,
                event TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_audit_log
            (id, owner_id, device_id, command_id, event, details, created_at)
            SELECT id, owner_id, device_id, command_id, event, details, created_at
            FROM agent_audit_log_phase6_legacy
            """
        )

        connection.execute("DROP TABLE agent_audit_log_phase6_legacy")
        connection.execute("DROP TABLE agent_approvals_phase6_legacy")
        connection.execute("DROP TABLE agent_commands_phase6_legacy")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_desktop_capabilities (
                device_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                capabilities TEXT NOT NULL DEFAULT '{}',
                session_type TEXT NOT NULL DEFAULT 'unknown',
                updated_at TEXT NOT NULL,
                UNIQUE(device_id, owner_id),
                FOREIGN KEY(device_id, owner_id)
                    REFERENCES agent_devices(id, owner_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_execution_sessions (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                device_id TEXT NOT NULL,
                command_id TEXT NOT NULL UNIQUE,
                adapter TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'started', 'completed', 'failed', 'cancelled'
                )),
                started_at TEXT NOT NULL,
                ended_at TEXT,
                FOREIGN KEY(command_id, owner_id)
                    REFERENCES agent_commands(id, owner_id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_agent_commands_owner ON agent_commands(owner_id, created_at)"
        )
        connection.execute(
            "CREATE INDEX idx_agent_approvals_owner ON agent_approvals(owner_id, status, expires_at)"
        )
        connection.execute(
            "CREATE INDEX idx_agent_audit_owner ON agent_audit_log(owner_id, created_at)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_execution_owner
            ON agent_execution_sessions(owner_id, started_at)
            """
        )

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Agent database migration found invalid ownership references")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _add_computer_use_registry(connection):
    actions = (
        "open_app", "capture_screen", "get_active_window", "input_text",
        "read_file", "write_file", "click", "browser_navigate",
        "browser_read_page", "browser_inspect_controls", "browser_fill",
        "browser_submit", "browser_download", "browser_close",
    )
    action_sql = ", ".join(f"'{action}'" for action in actions)
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE computer_observations (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                device_id TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('screen', 'browser_dom')),
                command_id TEXT UNIQUE,
                browser_session_id TEXT,
                page_identity TEXT,
                navigation_generation INTEGER,
                screen_width INTEGER NOT NULL CHECK(screen_width > 0),
                screen_height INTEGER NOT NULL CHECK(screen_height > 0),
                provider TEXT NOT NULL,
                image_sha256 TEXT,
                elements TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                UNIQUE(id, owner_id, device_id),
                FOREIGN KEY(device_id, owner_id)
                    REFERENCES agent_devices(id, owner_id) ON DELETE CASCADE,
                FOREIGN KEY(browser_session_id, owner_id, device_id)
                    REFERENCES browser_sessions(id, owner_id, device_id)
                    ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE browser_sessions (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                device_id TEXT NOT NULL,
                initial_url TEXT,
                page_identity TEXT,
                navigation_generation INTEGER,
                status TEXT NOT NULL CHECK(status IN (
                    'planned', 'active', 'closed', 'failed', 'expired'
                )),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                UNIQUE(id, owner_id, device_id),
                FOREIGN KEY(device_id, owner_id)
                    REFERENCES agent_devices(id, owner_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE computer_action_proposals (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                device_id TEXT NOT NULL,
                observation_id TEXT,
                browser_session_id TEXT,
                action TEXT NOT NULL,
                instruction TEXT NOT NULL,
                target_element_id TEXT,
                permission TEXT NOT NULL,
                confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                expected_result TEXT NOT NULL,
                verification_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(verification_status IN ('pending', 'verified', 'failed')),
                status TEXT NOT NULL CHECK(status IN (
                    'proposed', 'pending_approval', 'approved', 'executing',
                    'completed', 'failed', 'expired', 'cancelled'
                )),
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(id, owner_id),
                FOREIGN KEY(device_id, owner_id)
                    REFERENCES agent_devices(id, owner_id) ON DELETE CASCADE,
                FOREIGN KEY(observation_id, owner_id, device_id)
                    REFERENCES computer_observations(id, owner_id, device_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY(browser_session_id, owner_id, device_id)
                    REFERENCES browser_sessions(id, owner_id, device_id)
                    ON DELETE RESTRICT
            )
            """
        )

        connection.execute("ALTER TABLE agent_commands RENAME TO agent_commands_phase7_legacy")
        connection.execute("ALTER TABLE agent_approvals RENAME TO agent_approvals_phase7_legacy")
        connection.execute("ALTER TABLE agent_audit_log RENAME TO agent_audit_log_phase7_legacy")
        connection.execute(
            "ALTER TABLE agent_execution_sessions RENAME TO agent_execution_sessions_phase7_legacy"
        )
        connection.execute(
            f"""
            CREATE TABLE agent_commands (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                device_id TEXT NOT NULL,
                client_request_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ({action_sql})),
                payload TEXT NOT NULL,
                proposal_id TEXT UNIQUE,
                status TEXT NOT NULL CHECK(status IN (
                    'pending_approval', 'approved', 'denied', 'queued', 'executing',
                    'completed', 'failed', 'expired', 'cancelled'
                )),
                result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                UNIQUE(owner_id, client_request_id),
                UNIQUE(id, owner_id),
                FOREIGN KEY(device_id, owner_id)
                    REFERENCES agent_devices(id, owner_id) ON DELETE RESTRICT,
                FOREIGN KEY(proposal_id, owner_id)
                    REFERENCES computer_action_proposals(id, owner_id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_commands
            (id, owner_id, device_id, client_request_id, action, payload, status,
             result, created_at, updated_at, deadline_at)
            SELECT id, owner_id, device_id, client_request_id, action, payload, status,
                   result, created_at, updated_at, deadline_at
            FROM agent_commands_phase7_legacy
            """
        )
        connection.execute(
            """
            CREATE TABLE agent_approvals (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                command_id TEXT NOT NULL UNIQUE,
                token_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'denied', 'expired', 'cancelled')),
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                consumed_at TEXT,
                FOREIGN KEY(command_id, owner_id)
                    REFERENCES agent_commands(id, owner_id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_approvals
            (id, owner_id, command_id, token_hash, payload_hash, status,
             expires_at, created_at, consumed_at)
            SELECT id, owner_id, command_id, token_hash, payload_hash, status,
                   expires_at, created_at, consumed_at
            FROM agent_approvals_phase7_legacy
            """
        )
        connection.execute(
            """
            CREATE TABLE agent_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                device_id TEXT REFERENCES agent_devices(id) ON DELETE SET NULL,
                command_id TEXT REFERENCES agent_commands(id) ON DELETE SET NULL,
                event TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_audit_log
            (id, owner_id, device_id, command_id, event, details, created_at)
            SELECT id, owner_id, device_id, command_id, event, details, created_at
            FROM agent_audit_log_phase7_legacy
            """
        )
        connection.execute(
            """
            CREATE TABLE agent_execution_sessions (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                device_id TEXT NOT NULL,
                command_id TEXT NOT NULL UNIQUE,
                adapter TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'started', 'completed', 'failed', 'cancelled'
                )),
                started_at TEXT NOT NULL,
                ended_at TEXT,
                FOREIGN KEY(command_id, owner_id)
                    REFERENCES agent_commands(id, owner_id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_execution_sessions
            (id, owner_id, device_id, command_id, adapter, state, started_at, ended_at)
            SELECT id, owner_id, device_id, command_id, adapter, state, started_at, ended_at
            FROM agent_execution_sessions_phase7_legacy
            """
        )

        for table in (
            "agent_execution_sessions_phase7_legacy",
            "agent_audit_log_phase7_legacy",
            "agent_approvals_phase7_legacy",
            "agent_commands_phase7_legacy",
        ):
            connection.execute(f"DROP TABLE {table}")

        connection.execute(
            "CREATE INDEX idx_computer_observations_owner ON computer_observations(owner_id, captured_at)"
        )
        connection.execute(
            "CREATE INDEX idx_browser_sessions_owner ON browser_sessions(owner_id, status, expires_at)"
        )
        connection.execute(
            "CREATE INDEX idx_computer_proposals_owner ON computer_action_proposals(owner_id, status, expires_at)"
        )
        connection.execute(
            "CREATE INDEX idx_agent_commands_proposal ON agent_commands(proposal_id)"
        )
        connection.execute(
            "CREATE INDEX idx_agent_commands_owner ON agent_commands(owner_id, created_at)"
        )
        connection.execute(
            "CREATE INDEX idx_agent_approvals_owner ON agent_approvals(owner_id, status, expires_at)"
        )
        connection.execute(
            "CREATE INDEX idx_agent_audit_owner ON agent_audit_log(owner_id, created_at)"
        )
        connection.execute(
            """
            CREATE INDEX idx_agent_execution_owner
            ON agent_execution_sessions(owner_id, started_at)
            """
        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Computer-use migration found invalid ownership references")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _bind_desktop_click_observations(connection):
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(computer_observations)")
    }
    if "active_window_identity" not in columns:
        connection.execute(
            "ALTER TABLE computer_observations ADD COLUMN active_window_identity TEXT"
        )


def apply_migrations(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()

    versions = {
        row["version"]
        for row in connection.execute("SELECT version FROM schema_migrations")
    }
    if 1 not in versions:
        with connection:
            _create_phase1_tables(connection)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (1)")
        versions.add(1)

    if 2 not in versions:
        _rebuild_with_constraints(connection)
        with connection:
            connection.execute("INSERT INTO schema_migrations (version) VALUES (2)")
    if 3 not in versions:
        with connection:
            _add_rate_limit_scopes(connection)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (3)")
    if 4 not in versions:
        with connection:
            _bind_approval_hash_to_device(connection)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (4)")
    if 5 not in versions:
        with connection:
            _index_rate_event_expiry(connection)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (5)")
    if 6 not in versions:
        with connection:
            _expand_desktop_actions_and_add_registries(connection)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (6)")
    if 7 not in versions:
        with connection:
            _add_computer_use_registry(connection)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (7)")
    if 8 not in versions:
        with connection:
            _bind_desktop_click_observations(connection)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (8)")
