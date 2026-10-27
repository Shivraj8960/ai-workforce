import json
import sqlite3
import tempfile
from pathlib import Path

import main
from fastapi import HTTPException


CUSTOMER_A = {"id": "CUSTOMER-A"}
CUSTOMER_B = {"id": "CUSTOMER-B"}


def setup_database():
    temp_dir = tempfile.TemporaryDirectory()
    original_database = main.DATABASE
    main.DATABASE = str(Path(temp_dir.name) / "test.db")
    main.init_db()

    connection = main.get_db()
    connection.execute(
        "ALTER TABLE projects ADD COLUMN owner_id TEXT"
    ) if "owner_id" not in {
        row["name"]
        for row in connection.execute("PRAGMA table_info(projects)").fetchall()
    } else None

    connection.execute(
        """
        INSERT INTO projects (id, name, goal, owner_id)
        VALUES (?, ?, ?, ?)
        """,
        ("PROJECT-A", "Customer A project", "Test project", CUSTOMER_A["id"]),
    )
    connection.execute(
        """
        INSERT INTO projects (id, name, goal, owner_id)
        VALUES (?, ?, ?, ?)
        """,
        ("PROJECT-B", "Customer B project", "Test project", CUSTOMER_B["id"]),
    )
    connection.commit()
    connection.close()

    return temp_dir, original_database


def restore_database(temp_dir, original_database):
    main.DATABASE = original_database
    temp_dir.cleanup()


def test_creates_three_dependent_tasks():
    temp_dir, original_database = setup_database()
    try:
        result = main.create_research_report_plan(
            main.PlanRequest(goal="Evaluate renewable energy options"),
            customer=CUSTOMER_A,
        )

        assert result["status"] == "plan_created"
        assert result["workflow"] == "research_to_report"
        assert result["review_required"] is True
        assert result["ai_execution_started"] is False
        assert result["plan"]["total_tasks"] == 3

        tasks = result["plan"]["tasks"]
        assert [task.assigned_to for task in tasks] == [
            "Researcher", "Analyst", "Writer"
        ]
        assert tasks[0].dependencies == []
        assert tasks[1].dependencies == [tasks[0].id]
        assert tasks[2].dependencies == [tasks[0].id, tasks[1].id]
        assert all(task.owner_id == CUSTOMER_A["id"] for task in tasks)
        assert all(task.status == "pending" for task in tasks)

        connection = main.get_db()
        rows = connection.execute(
            "SELECT id, owner_id, dependencies FROM tasks ORDER BY id"
        ).fetchall()
        connection.close()

        assert len(rows) == 3
        assert all(row["owner_id"] == CUSTOMER_A["id"] for row in rows)
        print("PASS: three dependent tasks created with customer ownership")
    finally:
        restore_database(temp_dir, original_database)


def test_rejects_another_customers_project():
    temp_dir, original_database = setup_database()
    try:
        try:
            main.create_research_report_plan(
                main.PlanRequest(
                    goal="Research project security",
                    project_id="PROJECT-B",
                ),
                customer=CUSTOMER_A,
            )
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("Cross-customer project access was allowed")

        connection = main.get_db()
        count = connection.execute(
            "SELECT COUNT(*) FROM tasks"
        ).fetchone()[0]
        connection.close()

        assert count == 0
        print("PASS: cross-customer project access rejected")
    finally:
        restore_database(temp_dir, original_database)


def test_rolls_back_partial_task_creation():
    temp_dir, original_database = setup_database()
    original_builder = main.build_research_task_plan

    try:
        planned_tasks = original_builder("Test rollback")
        planned_tasks[1]["title"] = None
        main.build_research_task_plan = lambda goal: planned_tasks

        try:
            main.create_research_report_plan(
                main.PlanRequest(goal="Test rollback"),
                customer=CUSTOMER_A,
            )
        except HTTPException as exc:
            assert exc.status_code == 500
        else:
            raise AssertionError("Invalid task creation did not fail")

        connection = main.get_db()
        count = connection.execute(
            "SELECT COUNT(*) FROM tasks"
        ).fetchone()[0]
        connection.close()

        assert count == 0
        print("PASS: partial task creation rolled back")
    finally:
        main.build_research_task_plan = original_builder
        restore_database(temp_dir, original_database)


if __name__ == "__main__":
    test_creates_three_dependent_tasks()
    test_rejects_another_customers_project()
    test_rolls_back_partial_task_creation()
    print("ALL RESEARCH REPORT TESTS PASSED")
