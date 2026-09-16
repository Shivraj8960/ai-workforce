from agent_executor import execute_agent
from fastapi import FastAPI
from pydantic import BaseModel, Field
from typing import Optional, List
import sqlite3
import json


app = FastAPI(
    title="AI Workforce Platform",
    version="0.3.0",
    description="AI workforce task planning and execution API"
)


# =========================
# Database
# =========================

DATABASE = "workforce.db"


def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    connection = get_db()

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            assigned_to TEXT,
            status TEXT DEFAULT 'pending',
            priority TEXT DEFAULT 'medium',
            dependencies TEXT DEFAULT '[]',
            result TEXT
        )
        """
    )

    connection.commit()
    connection.close()


init_db()


# =========================
# Data Models
# =========================

class Employee(BaseModel):
    name: str
    role: str
    goal: str


class PlanRequest(BaseModel):
    goal: str


class Task(BaseModel):
    id: str
    title: str
    description: str = ""
    assigned_to: Optional[str] = None
    status: str = "pending"
    priority: str = "medium"
    dependencies: List[str] = Field(default_factory=list)
    result: Optional[str] = None


# =========================
# AI Employees
# =========================

employees_db = [
    Employee(
        name="Researcher",
        role="Research AI",
        goal="Find and organize useful information"
    ),
    Employee(
        name="Analyst",
        role="Analysis AI",
        goal="Analyze information and identify insights"
    ),
    Employee(
        name="Writer",
        role="Writing AI",
        goal="Create clear and personalized content"
    ),
    Employee(
        name="Executor",
        role="Execution AI",
        goal="Execute approved actions"
    ),
    Employee(
        name="Reviewer",
        role="Review AI",
        goal="Review results and identify improvements"
    )
]


# =========================
# Helper Functions
# =========================

def row_to_task(row):
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        assigned_to=row["assigned_to"],
        status=row["status"],
        priority=row["priority"],
        dependencies=json.loads(row["dependencies"]),
        result=row["result"]
    )


def save_task(task: Task):
    connection = get_db()

    connection.execute(
        """
        INSERT OR REPLACE INTO tasks
        (id, title, description, assigned_to, status,
         priority, dependencies, result)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.id,
            task.title,
            task.description,
            task.assigned_to,
            task.status,
            task.priority,
            json.dumps(task.dependencies),
            task.result
        )
    )

    connection.commit()
    connection.close()


def get_task_from_db(task_id: str):
    connection = get_db()

    row = connection.execute(
        "SELECT * FROM tasks WHERE id = ?",
        (task_id,)
    ).fetchone()

    connection.close()

    if row:
        return row_to_task(row)

    return None


# =========================
# Basic Endpoints
# =========================

@app.get("/")
def home():
    return {
        "message": "AI Workforce Platform is running",
        "version": "0.3.0",
        "database": "SQLite"
    }


@app.get("/health")
def health():
    connection = get_db()

    task_count = connection.execute(
        "SELECT COUNT(*) FROM tasks"
    ).fetchone()[0]

    connection.close()

    return {
        "status": "healthy",
        "employees": len(employees_db),
        "tasks": task_count,
        "database": "connected"
    }


# =========================
# Employee Endpoints
# =========================

@app.get("/employees")
def get_employees():
    return {
        "total_employees": len(employees_db),
        "employees": employees_db
    }


@app.post("/employees")
def create_employee(employee: Employee):
    employees_db.append(employee)

    return {
        "message": "AI Employee created successfully",
        "employee": employee
    }


# =========================
# Planning Engine
# =========================

@app.post("/plan")
def create_plan(request: PlanRequest):

    goal = request.goal.lower()

    if "sales" in goal or "meeting" in goal or "customer" in goal:

        task_data = [
            (
                "Find potential customers",
                "Identify potential customers matching the target profile",
                "Researcher",
                []
            ),
            (
                "Research and qualify prospects",
                "Research prospects and determine whether they are qualified",
                "Analyst",
                ["TASK-001"]
            ),
            (
                "Prepare personalized outreach",
                "Create personalized outreach messages for qualified prospects",
                "Writer",
                ["TASK-002"]
            ),
            (
                "Request human approval",
                "Send prepared outreach for human approval before external action",
                "Reviewer",
                ["TASK-003"]
            ),
            (
                "Send approved outreach",
                "Send only the outreach that has received human approval",
                "Executor",
                ["TASK-004"]
            ),
            (
                "Track responses",
                "Track prospect responses and engagement",
                "Analyst",
                ["TASK-005"]
            ),
            (
                "Update results",
                "Summarize outcomes and update the workforce results",
                "Reviewer",
                ["TASK-006"]
            )
        ]

    else:

        task_data = [
            (
                "Understand the goal",
                "Break the user's goal into actionable requirements",
                "Researcher",
                []
            ),
            (
                "Analyze requirements",
                "Analyze the requirements and determine the best approach",
                "Analyst",
                ["TASK-001"]
            ),
            (
                "Prepare execution plan",
                "Create a detailed plan for completing the goal",
                "Writer",
                ["TASK-002"]
            ),
            (
                "Review plan",
                "Review the plan before execution",
                "Reviewer",
                ["TASK-003"]
            ),
            (
                "Execute approved plan",
                "Execute the approved actions",
                "Executor",
                ["TASK-004"]
            ),
            (
                "Analyze results",
                "Analyze the execution results",
                "Analyst",
                ["TASK-005"]
            ),
            (
                "Finalize results",
                "Prepare the final result and summary",
                "Reviewer",
                ["TASK-006"]
            )
        ]

    created_tasks = []

    for index, (title, description, assigned_to, dependencies) in enumerate(
        task_data,
        start=1
    ):

        task = Task(
            id=f"TASK-{index:03d}",
            title=title,
            description=description,
            assigned_to=assigned_to,
            status="pending",
            priority="medium",
            dependencies=dependencies
        )

        save_task(task)
        created_tasks.append(task)

    return {
        "goal": request.goal,
        "status": "plan_created",
        "plan": {
            "total_tasks": len(created_tasks),
            "tasks": created_tasks
        }
    }


# =========================
# Task Endpoints
# =========================

@app.get("/tasks")
def get_tasks():

    connection = get_db()

    rows = connection.execute(
        "SELECT * FROM tasks ORDER BY rowid"
    ).fetchall()

    connection.close()

    tasks = [row_to_task(row) for row in rows]

    return {
        "total_tasks": len(tasks),
        "tasks": tasks
    }


@app.get("/tasks/{task_id}")
def get_task(task_id: str):

    task = get_task_from_db(task_id)

    if task:
        return task

    return {
        "error": "Task not found",
        "task_id": task_id
    }


@app.post("/tasks/{task_id}/execute")
def execute_task(task_id: str):

    task = get_task_from_db(task_id)

    if not task:
        return {
            "error": "Task not found",
            "task_id": task_id
        }

    # Check dependencies
    for dependency_id in task.dependencies:

        dependency = get_task_from_db(dependency_id)

        if dependency and dependency.status != "completed":
            return {
                "message": "Task is waiting for dependency",
                "task": task,
                "blocked_by": dependency_id
            }

    task.status = "running"
    save_task(task)

    # Phase 3 agent execution.
    execution = execute_agent(
        task.assigned_to,
        task.title,
        task.description,
    )

    task.result = execution["result"]

    task.status = "completed"
    save_task(task)

    return {
        "message": "Task executed successfully",
        "task": task,
        "result": task.result
    }


# =========================
# Workforce Status
# =========================

@app.get("/workforce")
def workforce_status():

    connection = get_db()

    total = connection.execute(
        "SELECT COUNT(*) FROM tasks"
    ).fetchone()[0]

    pending = connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'pending'"
    ).fetchone()[0]

    running = connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
    ).fetchone()[0]

    completed = connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'completed'"
    ).fetchone()[0]

    connection.close()

    return {
        "employees": len(employees_db),
        "tasks": {
            "total": total,
            "pending": pending,
            "running": running,
            "completed": completed
        },
        "database": "SQLite"
    }
