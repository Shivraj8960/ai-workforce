from source_validator import validate_sources
from agent_executor import execute_agent, generate_plan, extract_handoff_context
from self_healing import analyze_failure, should_retry, execute_with_retries
from auth import hash_password, verify_password, create_access_token, decode_access_token
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
import os
import secrets
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
import sqlite3
import uuid
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


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in {"/health", "/auth/register", "/auth/login"}:
        return await call_next(request)

    mode = os.getenv("NEX_ENV", "production").strip().lower()

    if mode == "development":
        return await call_next(request)

    if mode != "production":
        return JSONResponse(
            status_code=503,
            content={"detail": "Invalid server environment configuration"},
        )

    api_key = os.getenv("NEX_API_KEY", "")
    if not api_key:
        return JSONResponse(
            status_code=503,
            content={"detail": "API authentication is not configured"},
        )

    provided_key = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(provided_key, api_key):
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid API key"},
        )

    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)

    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self' http://127.0.0.1:8000 http://localhost:8000; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    return response


# =========================
# Customer Authentication Helpers
# =========================

customer_bearer = HTTPBearer(auto_error=False)

def get_current_customer(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(customer_bearer),
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        customer_id = decode_access_token(credentials.credentials)
    except (jwt.InvalidTokenError, RuntimeError):
        raise HTTPException(status_code=401, detail="Invalid or expired access token")

    connection = get_db()
    try:
        customer = connection.execute(
            """
            SELECT id, email, role, is_active
            FROM customers
            WHERE id = ?
            """,
            (customer_id,),
        ).fetchone()
    finally:
        connection.close()

    if customer is None or not customer["is_active"]:
        raise HTTPException(status_code=401, detail="Invalid or inactive account")

    return dict(customer)


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
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'customer',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    project_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(projects)").fetchall()
    }
    if "owner_id" not in project_columns:
        connection.execute("ALTER TABLE projects ADD COLUMN owner_id TEXT")

    task_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if "project_id" not in task_columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN project_id TEXT")
    if "owner_id" not in task_columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN owner_id TEXT")
    if "approval_status" not in task_columns:
        connection.execute(
            "ALTER TABLE tasks ADD COLUMN approval_status TEXT DEFAULT 'not_required'"
        )
    if "provider" not in task_columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN provider TEXT")

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
    project_id: Optional[str] = None


class ProjectCreate(BaseModel):
    name: str
    goal: str


class CustomerRegister(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=12, max_length=1024)


class CustomerLogin(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


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
    project_id: Optional[str] = None
    owner_id: Optional[str] = None




# =========================
# Customer Authentication
# =========================

@app.post("/auth/register", status_code=201)
def register_customer(request: CustomerRegister):
    mode = os.getenv("NEX_ENV", "production").strip().lower()
    registration_enabled = (
        mode == "development"
        or os.getenv("NEX_ALLOW_PUBLIC_REGISTRATION", "").strip().lower() == "true"
    )
    if not registration_enabled:
        raise HTTPException(status_code=403, detail="Registration is disabled")

    email = request.email.strip().lower()
    if (
        len(email) > 254
        or email.count("@") != 1
        or email.startswith("@")
        or email.endswith("@")
        or "." not in email.rsplit("@", 1)[-1]
        or any(char.isspace() for char in email)
    ):
        raise HTTPException(status_code=422, detail="Invalid email address")

    try:
        password_hash = hash_password(request.password)
        customer_id = f"CUSTOMER-{uuid.uuid4().hex[:16].upper()}"
        token = create_access_token(customer_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured",
        )

    connection = get_db()
    try:
        connection.execute(
            """
            INSERT INTO customers (id, email, password_hash)
            VALUES (?, ?, ?)
            """,
            (customer_id, email, password_hash),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Email is already registered")
    finally:
        connection.close()

    return {
        "status": "registered",
        "customer": {"id": customer_id, "email": email},
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 1800,
    }


@app.post("/auth/login")
def login_customer(request: CustomerLogin):
    email = request.email.strip().lower()
    connection = get_db()
    try:
        customer = connection.execute(
            """
            SELECT id, email, password_hash, is_active
            FROM customers
            WHERE email = ?
            """,
            (email,),
        ).fetchone()
    finally:
        connection.close()

    if (
        customer is None
        or not customer["is_active"]
        or not verify_password(request.password, customer["password_hash"])
    ):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    try:
        token = create_access_token(customer["id"])
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured",
        )

    return {
        "status": "authenticated",
        "customer": {"id": customer["id"], "email": customer["email"]},
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 1800,
    }

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
        project_id=row["project_id"] if "project_id" in row.keys() else None,
    )


def save_task(task: Task):
    connection = get_db()

    try:
        with connection:
            connection.execute(
                """
                INSERT INTO tasks
                (id, title, description, assigned_to, status,
                 priority, dependencies, result, approval_status, provider, project_id, owner_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    assigned_to = excluded.assigned_to,
                    status = excluded.status,
                    priority = excluded.priority,
                    dependencies = excluded.dependencies,
                    result = excluded.result,
                    approval_status = excluded.approval_status,
                    provider = excluded.provider,
                    project_id = excluded.project_id
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
                    task.project_id,
                    task.owner_id,
                ),
            )
    finally:
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
    
def get_task_from_db_for_owner(task_id: str, owner_id: str):
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
            (task_id, owner_id),
        ).fetchone()
    finally:
        connection.close()

    if row:
        return row_to_task(row)

    return None

def get_agent_memory(
    agent: str,
    limit: int = 5,
    owner_id: str | None = None,
):
    connection = get_db()

    if owner_id is None:
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
    else:
        rows = connection.execute(
            """
            SELECT am.task_id, am.memory, am.created_at
            FROM agent_memory am
            INNER JOIN tasks t ON t.id = am.task_id
            WHERE am.agent = ?
              AND t.owner_id = ?
            ORDER BY am.id DESC
            LIMIT ?
            """,
            (agent, owner_id, limit),
        ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


def save_agent_memory(agent: str, task_id: str, memory: str):
    if not memory:
        return

    connection = get_db()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO agent_memory
                (agent, task_id, memory)
                VALUES (?, ?, ?)
                """,
                (agent, task_id, memory),
            )
    finally:
        connection.close()


def save_execution_history(task_id: str, execution: dict):
    connection = get_db()
    try:
        with connection:
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
    finally:
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

@app.post("/projects")
def create_project(
    request: ProjectCreate,
    customer: dict = Depends(get_current_customer)
):
    name = request.name.strip()
    goal = request.goal.strip()

    if not name or not goal:
        raise HTTPException(
            status_code=400,
            detail="Project name and goal are required"
        )

    project_id = f"PROJECT-{uuid.uuid4().hex[:12].upper()}"
    connection = get_db()
    try:
        connection.execute(
            """
            INSERT INTO projects (id, name, goal, status, owner_id)
            VALUES (?, ?, ?, 'active', ?)
            """,
            (project_id, name, goal, customer["id"])
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM projects WHERE id = ? AND owner_id = ?",
            (project_id, customer["id"])
        ).fetchone()
    finally:
        connection.close()

    return {"status": "project_created", "project": dict(row)}


@app.get("/projects")
def get_projects(customer: dict = Depends(get_current_customer)):
    connection = get_db()
    try:
        rows = connection.execute(
            """
            SELECT p.*,
                COUNT(t.id) AS total_tasks,
                SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed_tasks
            FROM projects p
            LEFT JOIN tasks t ON t.project_id = p.id
            WHERE p.owner_id = ?
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """,
            (customer["id"],)
        ).fetchall()
    finally:
        connection.close()

    projects = []
    for row in rows:
        project = dict(row)
        project["completed_tasks"] = project["completed_tasks"] or 0
        projects.append(project)

    return {"total_projects": len(projects), "projects": projects}


@app.get("/projects/{project_id}")
def get_project(
    project_id: str,
    customer: dict = Depends(get_current_customer)
):
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT * FROM projects WHERE id = ? AND owner_id = ?",
            (project_id, customer["id"])
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Project not found")

        task_rows = connection.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY rowid",
            (project_id,)
        ).fetchall()
    finally:
        connection.close()

    tasks = [row_to_task(task_row) for task_row in task_rows]
    completed = sum(task.status == "completed" for task in tasks)

    return {
        "project": dict(row),
        "total_tasks": len(tasks),
        "completed_tasks": completed,
        "progress_percent": round(completed / len(tasks) * 100) if tasks else 0,
        "tasks": tasks
    }


@app.post("/plan")
def create_plan(
    request: PlanRequest,
    customer: dict = Depends(get_current_customer)
):
    if request.project_id:
        connection = get_db()
        try:
            project = connection.execute(
                "SELECT id FROM projects WHERE id = ? AND owner_id = ?",
                (request.project_id, customer["id"])
            ).fetchone()
        finally:
            connection.close()

        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

    try:
        raw_plan = generate_plan(request.goal)
        planned_tasks = json.loads(raw_plan)
    except Exception:
        return {
            "goal": request.goal,
            "status": "planning_failed",
            "error": "AI planning failed due to an internal error.",
        }

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
            dependencies=dependencies,
            project_id=request.project_id,
            owner_id=customer["id"]
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
def get_tasks(customer: dict = Depends(get_current_customer)):
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE owner_id = ? ORDER BY rowid",
            (customer["id"],)
        ).fetchall()
    finally:
        connection.close()

    tasks = [row_to_task(row) for row in rows]

    return {
        "total_tasks": len(tasks),
        "tasks": tasks
    }


@app.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    customer: dict = Depends(get_current_customer)
):
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
            (task_id, customer["id"])
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return row_to_task(row)


@app.get("/tasks/{task_id}/history")
def get_task_history(
    task_id: str,
    customer: dict = Depends(get_current_customer)
):
    connection = get_db()
    try:
        task = connection.execute(
            "SELECT id FROM tasks WHERE id = ? AND owner_id = ?",
            (task_id, customer["id"])
        ).fetchone()

        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")

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
    finally:
        connection.close()

    return {
        "task_id": task_id,
        "total_executions": len(rows),
        "history": [dict(row) for row in rows],
    }


@app.get("/agents/{agent}/history")
def get_agent_history(
    agent: str,
    customer: dict = Depends(get_current_customer),
):
    connection = get_db()

    rows = connection.execute(
        """
        SELECT eh.id, eh.task_id, eh.agent, eh.attempt, eh.provider,
               eh.status, eh.result, eh.failure_analysis, eh.recovery, eh.created_at
        FROM execution_history eh
        INNER JOIN tasks t ON t.id = eh.task_id
        WHERE LOWER(eh.agent) = LOWER(?)
          AND t.owner_id = ?
        ORDER BY eh.id DESC
        """,
        (agent, customer["id"]),
    ).fetchall()

    connection.close()

    return {
        "agent": agent,
        "total_executions": len(rows),
        "history": [dict(row) for row in rows],
    }


@app.get("/agents/{agent}/memory")
def get_agent_memory_endpoint(
    agent: str,
    customer: dict = Depends(get_current_customer),
):
    memories = get_agent_memory(agent, owner_id=customer["id"])

    return {
        "agent": agent,
        "total_memories": len(memories),
        "memories": memories,
    }



@app.post("/tasks/{task_id}/execute")
def execute_task(
    task_id: str,
    customer: dict = Depends(get_current_customer),
):
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
            (task_id, customer["id"]),
        ).fetchone()
    finally:
        connection.close()

    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Task not found")

    task = row_to_task(row)

    # Check dependencies
    for dependency_id in task.dependencies:

        dependency = get_task_from_db_for_owner(dependency_id, customer["id"])

        if dependency and dependency.status != "completed" and dependency.approval_status != "approved":
            return {
                "message": "Task is waiting for dependency",
                "task": task,
                "blocked_by": dependency_id
            }

    # Atomically claim pending or failed tasks before starting AI execution.
    connection = get_db()
    try:
        with connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = 'running'
                WHERE id = ?
  AND owner_id = ?
  AND status IN ('pending', 'failed')
  AND approval_status NOT IN ('approved', 'rejected')
                """,
                (task_id, customer["id"]),
            )
            claimed = cursor.rowcount == 1
    finally:
        connection.close()

    if not claimed:
        from fastapi import HTTPException
        latest_task = get_task_from_db_for_owner(task_id, customer["id"])
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Task is already running or is not eligible for execution",
                "task_id": task_id,
                "status": latest_task.status if latest_task else "not_found",
            },
        )

    try:
        task.status = "running"

        # Gather completed dependency results for agent-to-agent context.
        previous_results = []

        # Retrieve recent memory for the assigned agent.
        agent_memories = get_agent_memory(
            task.assigned_to,
            owner_id=customer["id"],
        )

        memory_context = ""
        if agent_memories:
            memory_context = "\n\nRelevant previous agent memory:\n" + "\n\n".join(
                f"Memory from task {item['task_id']} ({item['created_at']}):\n{item['memory']}"
                for item in agent_memories
            )
        for dependency_id in task.dependencies:
            dependency = get_task_from_db_for_owner(dependency_id, customer["id"])

            if dependency and dependency.result:
                dependency_execution = {}
                try:
                    parsed_execution = json.loads(dependency.result)
                    if isinstance(parsed_execution, dict):
                        dependency_execution = parsed_execution
                        dependency_output = parsed_execution.get("result", dependency.result)
                    else:
                        dependency_output = dependency.result
                except (TypeError, json.JSONDecodeError):
                    dependency_output = dependency.result

                handoff_context = extract_handoff_context(dependency_output)

                # Pass Researcher source accessibility results downstream.
                if isinstance(dependency_execution, dict):
                    source_validation = dependency_execution.get("source_validation")
                    if source_validation:
                        handoff_context += (
                            "\n\nResearcher source accessibility validation (not factual verification):\n"
                            + json.dumps(source_validation, ensure_ascii=False, indent=2)
                        )

                previous_results.append(
                    f"Previous task {dependency.id} ({dependency.title}) handoff:\n"
                    f"{handoff_context}"
                )

        # Phase 3 agent execution.
        execution = execute_agent(
            task.assigned_to,
            task.title,
            task.description
            + ("\n\nContext from completed previous tasks:\n" + "\n\n".join(previous_results) if previous_results else "")
            + memory_context,
        )

        # Validate sources only for successfully completed Researcher tasks.
        if (
            execution.get("status") == "completed"
            and str(execution.get("agent", task.assigned_to)).strip().lower() == "researcher"
        ):
            execution["source_validation"] = validate_sources(
                execution.get("result", "")
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
    except Exception:
        task.status = "failed"
        task.result = json.dumps({
            "status": "failed",
            "result": "Task execution failed due to an internal error.",
        })
        save_task(task)
        from fastapi import HTTPException
        raise HTTPException(
            status_code=500,
            detail="Task execution failed due to an internal error.",
        )



# =========================
# ============================
# Human Approval
# ============================

@app.post("/tasks/{task_id}/approve")
def approve_task(
    task_id: str,
    customer: dict = Depends(get_current_customer),
):
    from fastapi import HTTPException

    connection = get_db()
    try:
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
            (task_id, customer["id"]),
        ).fetchone()
    finally:
        connection.close()

    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    task = row_to_task(row)

    if task.approval_status != "awaiting_approval" or task.status != "pending":
        raise HTTPException(
            status_code=409,
            detail="Task is not awaiting approval",
        )

    connection = get_db()
    try:
        with connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET approval_status = 'approved', status = 'completed'
                WHERE id = ?
  AND owner_id = ?
  AND approval_status = 'awaiting_approval'
  AND status = 'pending'
                """,
                (task_id, customer["id"]),
            )
            if cursor.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Task state changed; approval was not applied",
                )
    finally:
        connection.close()

    return {
        "message": "Task approved",
        "task": get_task_from_db(task_id),
    }


@app.post("/tasks/{task_id}/reject")
def reject_task(
    task_id: str,
    customer: dict = Depends(get_current_customer),
):
    from fastapi import HTTPException

    connection = get_db()
    try:
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
            (task_id, customer["id"]),
        ).fetchone()
    finally:
        connection.close()

    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    task = row_to_task(row)

    if task.approval_status != "awaiting_approval" or task.status != "pending":
        raise HTTPException(
            status_code=409,
            detail="Task is not awaiting approval",
        )

    connection = get_db()
    try:
        with connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET approval_status = 'rejected', status = 'failed'
              WHERE id = ?
  AND owner_id = ?
  AND approval_status = 'awaiting_approval'
  AND status = 'pending'
                """,
                (task_id, customer["id"]),
            )
            if cursor.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Task state changed; rejection was not applied",
                )
    finally:
        connection.close()

    return {
        "message": "Task rejected",
        "task": get_task_from_db(task_id),
    }


# Workforce Status
# =========================

@app.get("/workforce")
def workforce_status(
    customer: dict = Depends(get_current_customer),
):

    connection = get_db()

    total = connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE owner_id = ?",
        (customer["id"],),
    ).fetchone()[0]

    pending = connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE owner_id = ? AND status = 'pending'",
        (customer["id"],),
    ).fetchone()[0]

    completed = connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE owner_id = ? AND status = 'completed'",
        (customer["id"],),
    ).fetchone()[0]

    running = connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE owner_id = ? AND status = 'running'",
        (customer["id"],),
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
def run_workforce(customer: dict = Depends(get_current_customer)):
    executed = []
    failed = []
    blocked = []

    while True:
        progress = False
        tasks = get_tasks(customer)["tasks"]

        for task in tasks:
            if task.status != "pending":
                continue

            dependencies_ready = True

            for dependency_id in task.dependencies:
                dependency = get_task_from_db_for_owner(
                    dependency_id,
                    customer["id"],
                )

                if not dependency or (dependency.status != "completed" and dependency.approval_status != "approved"):
                    dependencies_ready = False
                    break

            if not dependencies_ready:
                continue

            response = execute_task(task.id, customer)
            updated_task = get_task_from_db_for_owner(task.id, customer["id"])

            if updated_task:
                if updated_task.status == "completed":
                    executed.append(task.id)
                elif updated_task.status == "failed":
                    failed.append(task.id)

            progress = True

        if not progress:
            break

    for task in get_tasks(customer)["tasks"]:
        if task.status == "pending":
            blocked.append(task.id)

    return {
        "message": "Workforce execution completed",
        "executed": executed,
        "failed": failed,
        "blocked": blocked,
    }
