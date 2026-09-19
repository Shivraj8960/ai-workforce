from agent_executor import execute_agent, generate_plan
from self_healing import analyze_failure, should_retry, execute_with_retries
from fastapi import FastAPI
from pydantic import BaseModel, Field
from typing import Optional, List
import sqlite3
import json


app = FastAPI(
    title="Nex AI Platform",
    version="0.3.0",
    description="Nex AI task planning and execution API"
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
    approval_status: str = "not_required"
    provider: Optional[str] = None
    approval_status: str = "not_required"


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
        result=row["result"],
        approval_status=row["approval_status"],
        provider=row["provider"],
    )


def save_task(task: Task):
    connection = get_db()

    connection.execute(
        """
        INSERT OR REPLACE INTO tasks
        (id, title, description, assigned_to, status,
         priority, dependencies, result, approval_status, provider)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.id,
            task.title,
            task.description,
            task.assigned_to,
            task.status,
            task.priority,
            json.dumps(task.dependencies),
            task.result,
        task.approval_status,
            task.provider,
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
        "message": "Nex AI Platform is running",
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
    try:
        raw_plan = generate_plan(request.goal)
        planned_tasks = json.loads(raw_plan)
    except Exception as e:
        return {"goal": request.goal, "status": "planning_failed", "error": str(e)}

    if not isinstance(planned_tasks, list) or not 3 <= len(planned_tasks) <= 7:
        return {"goal": request.goal, "status": "planning_failed", "error": "AI planner must return between 3 and 7 tasks"}

    created_tasks = []

    for index, item in enumerate(planned_tasks, start=1):
        required = ["title", "description", "assigned_to", "dependencies"]
        if not all(key in item for key in required):
            return {"goal": request.goal, "status": "planning_failed", "error": "AI planner returned an invalid task format"}

        dependencies = [f"TASK-{int(dep):03d}" for dep in item["dependencies"]]

        task = Task(
            id=f"TASK-{index:03d}",
            title=item["title"],
            description=item["description"],
            assigned_to=item["assigned_to"],
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

        if dependency and dependency.status != "completed" and dependency.approval_status != "approved":
            return {
                "message": "Task is waiting for dependency",
                "task": task,
                "blocked_by": dependency_id
            }

    # Gather completed dependency results for agent-to-agent context.
    previous_results = []
    for dependency_id in task.dependencies:
        dependency = get_task_from_db(dependency_id)
        if dependency and dependency.result:
            previous_results.append(f"Previous task {dependency.id} ({dependency.title}):\n{dependency.result}")

    task.status = "running"
    save_task(task)

    # Phase 3 agent execution.
    execution = execute_with_retries(execute_agent,
        task.assigned_to,
        task.title,
        task.description + ("\n\nContext from completed previous tasks:\n" + "\n\n".join(previous_results) if previous_results else ""),
    )

    task.result = json.dumps(execution)
    task.provider = execution.get("provider")

    if execution["status"] == "completed":
        task.status = "completed"
    else:
        task.status = "failed"
    
    save_task(task)

    return {
        "message": "Task executed successfully" if task.status == "completed" else "Task execution failed",
        "task": task,
        "result": task.result
    }


# =========================
# ============================
# Human Approval
# ============================

@app.post("/tasks/{task_id}/approve")
def approve_task(task_id: str):
    task = get_task_from_db(task_id)

    if not task:
        return {
            "error": "Task not found",
            "task_id": task_id
        }

    task.approval_status = "approved"
    task.status = "completed"
    save_task(task)

    return {
        "message": "Task approved",
        "task": task
    }


@app.post("/tasks/{task_id}/reject")
def reject_task(task_id: str):
    task = get_task_from_db(task_id)

    if not task:
        return {
            "error": "Task not found",
            "task_id": task_id
        }

    task.approval_status = "rejected"
    task.status = "failed"
    save_task(task)

    return {
        "message": "Task rejected",
        "task": task
    }


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
@app.post("/workforce/run")
def run_workforce():
    executed = []
    failed = []
    blocked = []

    while True:
        progress = False
        tasks = get_tasks()["tasks"]

        for task in tasks:
            if task.status != "pending":
                continue

            dependencies_ready = True

            for dependency_id in task.dependencies:
                dependency = get_task_from_db(dependency_id)

                if not dependency or (dependency.status != "completed" and dependency.approval_status != "approved"):
                    dependencies_ready = False
                    break

            if not dependencies_ready:
                continue

            response = execute_task(task.id)
            updated_task = get_task_from_db(task.id)

            if updated_task:
                if updated_task.status == "completed":
                    executed.append(task.id)
                elif updated_task.status == "failed":
                    failed.append(task.id)

            progress = True

        if not progress:
            break

    for task in get_tasks()["tasks"]:
        if task.status == "pending":
            blocked.append(task.id)

    return {
        "message": "Workforce execution completed",
        "executed": executed,
        "failed": failed,
        "blocked": blocked,
    }
