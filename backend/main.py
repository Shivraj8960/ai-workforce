from agent_executor import execute_agent, generate_plan, extract_handoff_context
from self_healing import analyze_failure, should_retry, execute_with_retries
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
import sqlite3
import json


app = FastAPI(
    title="Nex AI Platform",
    version="0.3.0",
    description="Nex AI task planning and execution API"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5500", "http://localhost:5500", "http://127.0.0.1:5501", "http://localhost:5501"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            agent TEXT,
            attempt INTEGER NOT NULL,
            provider TEXT,
            status TEXT NOT NULL,
            result TEXT,
            failure_analysis TEXT,
            recovery TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            task_id TEXT,
            memory TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        name="Designer",
        role="Design AI",
        goal="Create layouts, UX concepts, and design specifications"
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


def get_agent_memory(agent: str, limit: int = 5):
    connection = get_db()

    rows = connection.execute(
        """
        SELECT task_id, memory, created_at
        FROM agent_memory
        WHERE agent = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (agent, limit),
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


def save_agent_memory(agent: str, task_id: str, memory: str):
    if not memory:
        return

    connection = get_db()

    connection.execute(
        """
        INSERT INTO agent_memory
        (agent, task_id, memory)
        VALUES (?, ?, ?)
        """,
        (agent, task_id, memory),
    )

    connection.commit()
    connection.close()


def save_execution_history(task_id: str, execution: dict):
    connection = get_db()

    connection.execute(
        """
        INSERT INTO execution_history
        (task_id, agent, attempt, provider, status, result, failure_analysis, recovery)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            execution.get("agent"),
            execution.get("attempts", 1),
            execution.get("provider"),
            execution.get("status", "unknown"),
            execution.get("result"),
            json.dumps(execution.get("failure_analysis")) if execution.get("failure_analysis") else None,
            execution.get("recovery"),
        ),
    )

    connection.commit()
    connection.close()


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

    connection = get_db()
    existing_task_ids = connection.execute(
        "SELECT id FROM tasks WHERE id LIKE 'TASK-%'"
    ).fetchall()
    connection.close()

    existing_numbers = []
    for row in existing_task_ids:
        try:
            existing_numbers.append(int(row["id"].split("-")[1]))
        except (IndexError, ValueError):
            continue

    next_task_number = max(existing_numbers, default=0) + 1
    task_id_map = {
        index: f"TASK-{next_task_number + index - 1:03d}"
        for index in range(1, len(planned_tasks) + 1)
    }

    created_tasks = []

    for index, item in enumerate(planned_tasks, start=1):
        required = ["title", "description", "assigned_to", "dependencies"]
        if not all(key in item for key in required):
            return {"goal": request.goal, "status": "planning_failed", "error": "AI planner returned an invalid task format"}

        try:
            dependency_numbers = [int(dep) for dep in item["dependencies"]]
            dependencies = [task_id_map[dep] for dep in dependency_numbers]
        except (TypeError, ValueError, KeyError):
            return {"goal": request.goal, "status": "planning_failed", "error": "AI planner returned invalid dependency references"}

        task = Task(
            id=task_id_map[index],
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


@app.get("/tasks/{task_id}/history")
def get_task_history(task_id: str):
    connection = get_db()

    rows = connection.execute(
        """
        SELECT id, task_id, agent, attempt, provider, status,
               result, failure_analysis, recovery, created_at
        FROM execution_history
        WHERE task_id = ?
        ORDER BY id DESC
        """,
        (task_id,),
    ).fetchall()

    connection.close()

    return {
        "task_id": task_id,
        "total_executions": len(rows),
        "history": [dict(row) for row in rows],
    }



@app.get("/agents/{agent}/memory")
def get_agent_memory_endpoint(agent: str):
    memories = get_agent_memory(agent)

    return {
        "agent": agent,
        "total_memories": len(memories),
        "memories": memories,
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

    # Retrieve recent memory for the assigned agent.
    agent_memories = get_agent_memory(task.assigned_to)

    memory_context = ""
    if agent_memories:
        memory_context = "\n\nRelevant previous agent memory:\n" + "\n\n".join(
            f"Memory from task {item['task_id']} ({item['created_at']}):\n{item['memory']}"
            for item in agent_memories
        )
    for dependency_id in task.dependencies:
        dependency = get_task_from_db(dependency_id)

        if dependency and dependency.result:
            try:
                dependency_execution = json.loads(dependency.result)
                dependency_output = dependency_execution.get("result", dependency.result)
            except (TypeError, json.JSONDecodeError):
                dependency_output = dependency.result

            handoff_context = extract_handoff_context(dependency_output)

            previous_results.append(
                f"Previous task {dependency.id} ({dependency.title}) handoff:\n"
                f"{handoff_context}"
            )

    task.status = "running"
    save_task(task)

    # Phase 3 agent execution.
    execution = execute_agent(
        task.assigned_to,
        task.title,
        task.description
        + ("\n\nContext from completed previous tasks:\n" + "\n\n".join(previous_results) if previous_results else "")
        + memory_context,
    )

    task.result = json.dumps(execution)
    task.provider = execution.get("provider")
    save_execution_history(task.id, execution)
    if execution.get("status") == "completed":
        save_agent_memory(execution.get("agent", task.assigned_to), task.id, execution.get("result", ""))

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
