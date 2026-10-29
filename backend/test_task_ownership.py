
import inspect
import sqlite3
import tempfile
from pathlib import Path

import main


CUSTOMER_A = {"id": "CUSTOMER-A", "email": "a@test.local", "role": "customer", "is_active": 1}
CUSTOMER_B = {"id": "CUSTOMER-B", "email": "b@test.local", "role": "customer", "is_active": 1}


def setup_database():
    temp_dir = tempfile.TemporaryDirectory()
    db_path = Path(temp_dir.name) / "test.db"

    original_database = main.DATABASE
    main.DATABASE = str(db_path)

    connection = sqlite3.connect(main.DATABASE)
    connection.row_factory = sqlite3.Row

    connection.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            assigned_to TEXT,
            status TEXT DEFAULT 'pending',
            priority TEXT DEFAULT 'medium',
            dependencies TEXT DEFAULT '[]',
            result TEXT,
            project_id TEXT,
            owner_id TEXT,
            approval_status TEXT DEFAULT 'not_required',
            provider TEXT
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE agent_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            task_id TEXT,
            memory TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        INSERT INTO tasks
        (id, title, owner_id, status, approval_status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "TASK-A",
            "Customer A approval task",
            CUSTOMER_A["id"],
            "pending",
            "awaiting_approval",
        ),
    )

    connection.commit()
    connection.close()

    return temp_dir, original_database


def restore_database(temp_dir, original_database):
    main.DATABASE = original_database
    temp_dir.cleanup()


def test_approval_and_rejection_ownership():
    temp_dir, original_database = setup_database()

    try:
        # Customer B must NOT be able to approve Customer A's task.
        try:
            main.approve_task("TASK-A", CUSTOMER_B)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 404
        else:
            raise AssertionError("Customer B was able to approve Customer A's task")

        # Customer A can approve their own task.
        result = main.approve_task("TASK-A", CUSTOMER_A)
        assert result["message"] == "Task approved"

        connection = main.get_db()
        row = connection.execute(
            "SELECT status, approval_status FROM tasks WHERE id = ?",
            ("TASK-A",),
        ).fetchone()
        connection.close()

        assert row["status"] == "completed"
        assert row["approval_status"] == "approved"

        # Reset task for rejection test.
        connection = main.get_db()
        connection.execute(
            """
            UPDATE tasks
            SET status = 'pending',
                approval_status = 'awaiting_approval'
            WHERE id = ?
            """,
            ("TASK-A",),
        )
        connection.commit()
        connection.close()

        # Customer B must NOT be able to reject Customer A's task.
        try:
            main.reject_task("TASK-A", CUSTOMER_B)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 404
        else:
            raise AssertionError("Customer B was able to reject Customer A's task")

        # Customer A can reject their own task.
        result = main.reject_task("TASK-A", CUSTOMER_A)
        assert result["message"] == "Task rejected"

        connection = main.get_db()
        row = connection.execute(
            "SELECT status, approval_status FROM tasks WHERE id = ?",
            ("TASK-A",),
        ).fetchone()
        connection.close()

        assert row["status"] == "failed"
        assert row["approval_status"] == "rejected"

        print("PASS: approval/rejection ownership checks")

    finally:
        restore_database(temp_dir, original_database)


def test_execute_requires_customer_ownership():
    signature = inspect.signature(main.execute_task)

    assert "customer" in signature.parameters, (
        "execute_task must require an authenticated customer "
        "before ownership testing can proceed"
    )

    print("PASS: execute_task requires customer ownership context")


def test_agent_memory_ownership():
    temp_dir, original_database = setup_database()

    try:
        connection = main.get_db()

        connection.execute(
            """
            INSERT INTO tasks
            (id, title, owner_id, status, approval_status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "TASK-B",
                "Customer B memory task",
                CUSTOMER_B["id"],
                "completed",
                "approved",
            ),
        )

        connection.execute(
            """
            INSERT INTO agent_memory
            (agent, task_id, memory)
            VALUES (?, ?, ?)
            """,
            ("Researcher", "TASK-A", "Memory belonging to Customer A"),
        )

        connection.execute(
            """
            INSERT INTO agent_memory
            (agent, task_id, memory)
            VALUES (?, ?, ?)
            """,
            ("Researcher", "TASK-B", "Memory belonging to Customer B"),
        )

        connection.commit()
        connection.close()

        memories_a = main.get_agent_memory(
            "Researcher",
            owner_id=CUSTOMER_A["id"],
        )

        memories_b = main.get_agent_memory(
            "Researcher",
            owner_id=CUSTOMER_B["id"],
        )

        assert len(memories_a) == 1
        assert memories_a[0]["memory"] == "Memory belonging to Customer A"

        assert len(memories_b) == 1
        assert memories_b[0]["memory"] == "Memory belonging to Customer B"

        print("PASS: agent memory ownership isolation")

    finally:
        restore_database(temp_dir, original_database)
def test_workforce_run_is_owner_scoped():
    temp_dir, original_database = setup_database()
    original_execute_task = main.execute_task

    try:
        connection = main.get_db()
        connection.execute(
            """
            INSERT INTO tasks
            (id, title, owner_id, status, approval_status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "TASK-B",
                "Customer B workforce task",
                CUSTOMER_B["id"],
                "pending",
                "not_required",
            ),
        )
        connection.commit()
        connection.close()

        executed_task_ids = []

        def fake_execute_task(task_id, customer):
            executed_task_ids.append((task_id, customer["id"]))

            connection = main.get_db()
            try:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'completed'
                    WHERE id = ? AND owner_id = ?
                    """,
                    (task_id, customer["id"]),
                )
                connection.commit()
            finally:
                connection.close()

            return {
                "message": "Task executed successfully",
                "task": main.get_task_from_db_for_owner(
                    task_id,
                    customer["id"],
                ),
                "result": "mocked execution",
            }

        main.execute_task = fake_execute_task

        result = main.run_workforce(CUSTOMER_A)

        assert ("TASK-A", CUSTOMER_A["id"]) in executed_task_ids
        assert ("TASK-B", CUSTOMER_B["id"]) not in executed_task_ids
        assert all(owner_id == CUSTOMER_A["id"] for _, owner_id in executed_task_ids)

        print("PASS: workforce run ownership isolation")
    finally:
        main.execute_task = original_execute_task
        restore_database(temp_dir, original_database)
def test_execute_rejects_unavailable_dependencies():
    from fastapi import HTTPException

    cases = [
        ("MISSING-DEP", None),
        ("TASK-B", "other_customer"),
        ("TASK-DEP", "incomplete"),
    ]

    for dependency_id, dependency_type in cases:
        temp_dir, original_database = setup_database()
        original_execute_agent = main.execute_agent
        agent_calls = []

        def fake_execute_agent(*args, **kwargs):
            agent_calls.append(args)
            return {
                "status": "completed",
                "agent": "Writer",
                "provider": "mock",
                "result": "mock result",
            }

        try:
            connection = main.get_db()

            if dependency_type == "other_customer":
                connection.execute(
                    """
                    INSERT INTO tasks
                    (id, title, owner_id, status, approval_status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        dependency_id,
                        "Private task",
                        CUSTOMER_B["id"],
                        "completed",
                        "approved",
                    ),
                )

            elif dependency_type == "incomplete":
                connection.execute(
                    """
                    INSERT INTO tasks
                    (id, title, owner_id, status, approval_status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        dependency_id,
                        "Incomplete dependency",
                        CUSTOMER_A["id"],
                        "pending",
                        "not_required",
                    ),
                )

            connection.execute(
                "UPDATE tasks SET dependencies = ? WHERE id = ?",
                (main.json.dumps([dependency_id]), "TASK-A"),
            )
            connection.commit()
            connection.close()

            main.execute_agent = fake_execute_agent

            try:
                main.execute_task("TASK-A", CUSTOMER_A)
            except HTTPException as exc:
                assert exc.status_code == 409
                assert dependency_id not in str(exc.detail) or (
                    dependency_type != "other_customer"
                )
            else:
                raise AssertionError(
                    f"Expected HTTP 409 for dependency case: {dependency_type}"
                )

            assert agent_calls == [], (
                "AI execution must not start when a dependency is unavailable "
                "or incomplete"
            )

        finally:
            main.execute_agent = original_execute_agent
            restore_database(temp_dir, original_database)

    print("PASS: missing, inaccessible and incomplete dependencies rejected")

if __name__ == "__main__":
    test_approval_and_rejection_ownership()
    test_execute_requires_customer_ownership()
    test_agent_memory_ownership()
    test_workforce_run_is_owner_scoped()
    test_execute_rejects_unavailable_dependencies()
    print("ALL TASK OWNERSHIP TESTS PASSED")
