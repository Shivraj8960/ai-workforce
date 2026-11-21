from source_validator import validate_sources
from agent_executor import execute_agent, generate_plan, extract_handoff_context
from research_report import build_research_task_plan
from self_healing import analyze_failure, should_retry, execute_with_retries
from auth import hash_password, verify_password, create_access_token, decode_access_token
from local_agent_protocol import ALLOWED_ACTIONS as LOCAL_AGENT_ALLOWED_ACTIONS
from local_agent_protocol import validate_command
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
import os
import secrets
import asyncio
import hashlib
import hmac
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, StrictBool, StrictStr, ValidationError
from typing import Optional, List, Dict, Any, Literal
import sqlite3
import uuid
import json
from datetime import datetime, timedelta, timezone


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
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_commands_owner ON agent_commands(owner_id, created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_approvals_owner ON agent_approvals(owner_id, status, expires_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_audit_owner ON agent_audit_log(owner_id, created_at)"
    )

    startup_time = datetime.now(timezone.utc).isoformat()
    stale_commands = connection.execute(
        """
        SELECT id, owner_id, device_id, action FROM agent_commands
        WHERE status = 'dispatched'
        """
    ).fetchall()
    for row in stale_commands:
        result = {
            "command_id": row["id"],
            "action": row["action"],
            "success": False,
            "message": "Backend restarted before receiving a Local Agent result",
        }
        connection.execute(
            """
            UPDATE agent_commands
            SET status = 'failed', result = ?, updated_at = ?
            WHERE id = ? AND status = 'dispatched'
            """,
            (json.dumps(result), startup_time, row["id"]),
        )
        connection.execute(
            """
            INSERT INTO agent_audit_log
            (owner_id, device_id, command_id, event, details, created_at)
            VALUES (?, ?, ?, 'command_failed', ?, ?)
            """,
            (
                row["owner_id"],
                row["device_id"],
                row["id"],
                json.dumps({"reason": "backend restarted"}),
                startup_time,
            ),
        )
    connection.execute(
        "UPDATE agent_devices SET status = 'disconnected' WHERE status = 'connected'"
    )

    connection.commit()
    connection.close()


init_db()


DESKTOP_EXECUTION_ENABLED = False
AGENT_COMMAND_TIMEOUT_SECONDS = 15
AGENT_APPROVAL_TTL_SECONDS = 300
AGENT_REQUESTS_PER_MINUTE = 20
ALLOWED_AGENT_ACTIONS = set(LOCAL_AGENT_ALLOWED_ACTIONS)
AGENT_APP_ALLOWLIST = LOCAL_AGENT_ALLOWED_ACTIONS["open_app"]["app_id"]
AGENT_CONNECTIONS = {}
PENDING_AGENT_COMMANDS = {}


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


class AgentDeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    permissions: List[str] = Field(default_factory=list, max_length=2)

    class Config:
        extra = "forbid"


class AgentCommandRequest(BaseModel):
    action: Literal["open_app", "capture_screen"]
    payload: Dict[str, Any] = Field(default_factory=dict)
    client_request_id: str = Field(min_length=16, max_length=128)

    class Config:
        extra = "forbid"


class AgentApprovalDecision(BaseModel):
    approval_token: StrictStr = Field(min_length=32, max_length=128)

    class Config:
        extra = "forbid"


class AgentCommandResult(BaseModel):
    command_id: StrictStr = Field(min_length=1, max_length=64)
    action: Literal["open_app", "capture_screen"]
    success: StrictBool
    message: StrictStr = Field(min_length=1, max_length=500)

    class Config:
        extra = "forbid"


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
    dependencies_raw = row["dependencies"]
    try:
        dependencies = json.loads(dependencies_raw)
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            dependencies = []
    except (TypeError, json.JSONDecodeError):
        dependencies = []

    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        assigned_to=row["assigned_to"],
        status=row["status"],
        priority=row["priority"],
        dependencies=dependencies,
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


@app.post("/research/report-plan")
def create_research_report_plan(
    request: PlanRequest,
    customer: dict = Depends(get_current_customer)
):
    goal = request.goal.strip() if isinstance(request.goal, str) else ""
    if not goal:
        raise HTTPException(status_code=422, detail="Research goal cannot be empty")

    planned_tasks = build_research_task_plan(goal)
    connection = get_db()
    created_tasks = []

    try:
        connection.execute("BEGIN IMMEDIATE")

        if request.project_id:
            project = connection.execute(
                "SELECT id FROM projects WHERE id = ? AND owner_id = ?",
                (request.project_id, customer["id"])
            ).fetchone()
            if project is None:
                raise HTTPException(status_code=404, detail="Project not found")

        rows = connection.execute(
            "SELECT id FROM tasks WHERE id LIKE 'TASK-%'"
        ).fetchall()

        numbers = []
        for row in rows:
            try:
                numbers.append(int(row["id"].split("-")[1]))
            except (IndexError, ValueError):
                continue

        first_number = max(numbers, default=0) + 1
        task_id_map = {
            i: f"TASK-{first_number + i - 1:03d}"
            for i in range(1, len(planned_tasks) + 1)
        }

        for index, item in enumerate(planned_tasks, start=1):
            dependencies = [
                task_id_map[int(dep)] for dep in item["dependencies"]
            ]
            task = Task(
                id=task_id_map[index],
                title=item["title"],
                description=item["description"],
                assigned_to=item["assigned_to"],
                status="pending",
                priority="medium",
                dependencies=dependencies,
                project_id=request.project_id,
                owner_id=customer["id"],
            )

            connection.execute(
                """
                INSERT INTO tasks
                (id, title, description, assigned_to, status, priority,
                 dependencies, result, approval_status, provider,
                 project_id, owner_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id, task.title, task.description, task.assigned_to,
                    task.status, task.priority, json.dumps(task.dependencies),
                    task.result, task.approval_status, task.provider,
                    task.project_id, task.owner_id,
                ),
            )
            created_tasks.append(task)

        connection.commit()
    except HTTPException:
        connection.rollback()
        raise
    except Exception:
        connection.rollback()
        raise HTTPException(
            status_code=500,
            detail="Unable to create the research report plan",
        )
    finally:
        connection.close()

    return {
        "goal": goal,
        "status": "plan_created",
        "workflow": "research_to_report",
        "review_required": True,
        "ai_execution_started": False,
        "plan": {"total_tasks": len(created_tasks), "tasks": created_tasks},
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


# =========================
# Paired Local Agent
# =========================

def _utc_now():
    return datetime.now(timezone.utc)


def _utc_text(value=None):
    return (value or _utc_now()).isoformat()


def _agent_audit(connection, owner_id, event, device_id=None, command_id=None, details=None):
    connection.execute(
        """
        INSERT INTO agent_audit_log
        (owner_id, device_id, command_id, event, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            owner_id,
            device_id,
            command_id,
            event,
            json.dumps(details or {}, separators=(",", ":"), sort_keys=True),
            _utc_text(),
        ),
    )


def _agent_safety_stopped(connection, owner_id):
    row = connection.execute(
        "SELECT emergency_stopped FROM agent_safety WHERE owner_id = ?",
        (owner_id,),
    ).fetchone()
    return bool(row["emergency_stopped"]) if row else False


def _validate_agent_action(action, payload):
    if action not in ALLOWED_AGENT_ACTIONS or not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Action or payload is invalid")

    if action == "open_app":
        if (
            set(payload) != {"app_id"}
            or not isinstance(payload.get("app_id"), str)
            or payload["app_id"] not in AGENT_APP_ALLOWLIST
        ):
            raise HTTPException(status_code=422, detail="Application is not allowlisted")
    elif payload:
        raise HTTPException(status_code=422, detail="capture_screen does not accept a payload")


def _expire_agent_approvals(connection, owner_id):
    now = _utc_text()
    expired = connection.execute(
        """
        SELECT id, command_id FROM agent_approvals
        WHERE owner_id = ? AND status = 'pending' AND expires_at <= ?
        """,
        (owner_id, now),
    ).fetchall()
    if expired:
        connection.executemany(
            """
            UPDATE agent_approvals SET status = 'expired'
            WHERE id = ? AND status = 'pending'
            """,
            [(row["id"],) for row in expired],
        )
        connection.executemany(
            """
            UPDATE agent_commands
            SET status = 'expired', updated_at = ?
            WHERE id = ? AND status = 'awaiting_approval'
            """,
            [(now, row["command_id"]) for row in expired],
        )
        for row in expired:
            _agent_audit(
                connection,
                owner_id,
                "approval_expired",
                command_id=row["command_id"],
            )


def _wake_cancelled_agent_commands(owner_id, message, device_id=None):
    connection = get_db()
    try:
        query = """
            SELECT id, action FROM agent_commands
            WHERE owner_id = ? AND status = 'cancelled'
        """
        params = [owner_id]
        if device_id is not None:
            query += " AND device_id = ?"
            params.append(device_id)
        rows = connection.execute(query, params).fetchall()
    finally:
        connection.close()

    for row in rows:
        future = PENDING_AGENT_COMMANDS.get(row["id"])
        if future is not None and not future.done():
            future.set_result({
                "command_id": row["id"],
                "action": row["action"],
                "success": False,
                "message": message,
            })


@app.post("/computer/agent/devices", status_code=201)
def pair_agent_device(
    request: AgentDeviceCreate,
    customer: dict = Depends(get_current_customer),
):
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Device name cannot be empty")
    if len(set(request.permissions)) != len(request.permissions) or any(
        permission not in ALLOWED_AGENT_ACTIONS for permission in request.permissions
    ):
        raise HTTPException(status_code=422, detail="Device permissions are invalid")

    device_id = str(uuid.uuid4())
    credential = secrets.token_urlsafe(32)
    connection = get_db()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO agent_devices
                (id, owner_id, name, credential_hash, permissions, status)
                VALUES (?, ?, ?, ?, ?, 'paired')
                """,
                (
                    device_id,
                    customer["id"],
                    name,
                    hashlib.sha256(credential.encode("utf-8")).hexdigest(),
                    json.dumps(request.permissions),
                ),
            )
            _agent_audit(
                connection,
                customer["id"],
                "device_paired",
                device_id=device_id,
                details={"permissions": request.permissions},
            )
    finally:
        connection.close()

    return {
        "device": {
            "id": device_id,
            "name": name,
            "permissions": request.permissions,
            "status": "paired",
        },
        "device_credential": credential,
        "credential_notice": "Store this credential securely; it is shown only once.",
    }


@app.get("/computer/agent/devices")
def list_agent_devices(customer: dict = Depends(get_current_customer)):
    connection = get_db()
    try:
        rows = connection.execute(
            """
            SELECT id, name, permissions, status, created_at, last_seen_at
            FROM agent_devices WHERE owner_id = ? ORDER BY created_at DESC
            """,
            (customer["id"],),
        ).fetchall()
    finally:
        connection.close()

    return {
        "devices": [
            {
                **dict(row),
                "permissions": json.loads(row["permissions"]),
            }
            for row in rows
        ]
    }


@app.post("/computer/agent/devices/{device_id}/revoke")
async def revoke_agent_device(
    device_id: str,
    customer: dict = Depends(get_current_customer),
):
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        device = connection.execute(
            "SELECT id FROM agent_devices WHERE id = ? AND owner_id = ?",
            (device_id, customer["id"]),
        ).fetchone()
        if device is None:
            connection.rollback()
            raise HTTPException(status_code=404, detail="Device not found")
        connection.execute(
            "UPDATE agent_devices SET status = 'revoked' WHERE id = ? AND owner_id = ?",
            (device_id, customer["id"]),
        )
        connection.execute(
            """
            UPDATE agent_approvals SET status = 'denied', consumed_at = ?
            WHERE owner_id = ? AND status = 'pending'
              AND command_id IN (
                  SELECT id FROM agent_commands
                  WHERE device_id = ? AND status = 'awaiting_approval'
              )
            """,
            (_utc_text(), customer["id"], device_id),
        )
        connection.execute(
            """
            UPDATE agent_commands
            SET status = 'cancelled', updated_at = ?
            WHERE owner_id = ? AND device_id = ?
              AND status IN ('awaiting_approval', 'dispatched')
            """,
            (_utc_text(), customer["id"], device_id),
        )
        _agent_audit(
            connection,
            customer["id"],
            "device_revoked",
            device_id=device_id,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    websocket = AGENT_CONNECTIONS.pop(device_id, None)
    connection_closed = True
    if websocket is not None:
        try:
            await websocket.close(code=1008, reason="Device revoked")
        except (RuntimeError, WebSocketDisconnect):
            connection_closed = False
            connection = get_db()
            try:
                with connection:
                    _agent_audit(
                        connection,
                        customer["id"],
                        "device_socket_close_failed",
                        device_id=device_id,
                    )
            finally:
                connection.close()

    _wake_cancelled_agent_commands(
        customer["id"],
        "Device was revoked",
        device_id=device_id,
    )

    return {
        "device_id": device_id,
        "status": "revoked",
        "connection_closed": connection_closed,
    }


@app.post("/computer/agent/emergency-stop")
async def emergency_stop(customer: dict = Depends(get_current_customer)):
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO agent_safety (owner_id, emergency_stopped, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(owner_id) DO UPDATE SET
                emergency_stopped = 1, updated_at = excluded.updated_at
            """,
            (customer["id"], _utc_text()),
        )
        connection.execute(
            """
            UPDATE agent_approvals
            SET status = 'denied', consumed_at = ?
            WHERE owner_id = ? AND status = 'pending'
            """,
            (_utc_text(), customer["id"]),
        )
        connection.execute(
            """
            UPDATE agent_commands
            SET status = 'cancelled', updated_at = ?
            WHERE owner_id = ? AND status IN ('awaiting_approval', 'dispatched')
            """,
            (_utc_text(), customer["id"]),
        )
        connection.execute(
            """
            UPDATE agent_devices SET status = 'disconnected'
            WHERE owner_id = ? AND status = 'connected'
            """,
            (customer["id"],),
        )
        _agent_audit(connection, customer["id"], "emergency_stop")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    owned_connections = []
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT id FROM agent_devices WHERE owner_id = ?",
            (customer["id"],),
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        websocket = AGENT_CONNECTIONS.pop(row["id"], None)
        if websocket is not None:
            owned_connections.append(websocket)
    failed_closes = 0
    for websocket in owned_connections:
        try:
            await websocket.close(code=1008, reason="Emergency stop")
        except (RuntimeError, WebSocketDisconnect):
            failed_closes += 1

    if failed_closes:
        connection = get_db()
        try:
            with connection:
                _agent_audit(
                    connection,
                    customer["id"],
                    "emergency_stop_socket_close_failed",
                    details={"count": failed_closes},
                )
        finally:
            connection.close()

    _wake_cancelled_agent_commands(customer["id"], "Emergency stop activated")

    return {"status": "stopped", "message": "All Local Agent commands were stopped"}


@app.post("/computer/agent/emergency-stop/reset")
def reset_emergency_stop(customer: dict = Depends(get_current_customer)):
    connection = get_db()
    try:
        with connection:
            connection.execute(
                """
                UPDATE agent_safety SET emergency_stopped = 0, updated_at = ?
                WHERE owner_id = ?
                """,
                (_utc_text(), customer["id"]),
            )
            _agent_audit(connection, customer["id"], "emergency_stop_reset")
    finally:
        connection.close()
    return {"status": "active", "message": "Emergency stop reset; devices must reconnect"}


@app.post("/computer/agent/devices/{device_id}/commands", status_code=202)
def request_agent_command(
    device_id: str,
    request: AgentCommandRequest,
    customer: dict = Depends(get_current_customer),
):
    _validate_agent_action(request.action, request.payload)
    connection = get_db()
    try:
        device = connection.execute(
            """
            SELECT id, permissions, status FROM agent_devices
            WHERE id = ? AND owner_id = ?
            """,
            (device_id, customer["id"]),
        ).fetchone()
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        if _agent_safety_stopped(connection, customer["id"]):
            _agent_audit(connection, customer["id"], "command_rejected_emergency_stop", device_id=device_id)
            connection.commit()
            raise HTTPException(status_code=423, detail="Emergency stop is active")
        if device["status"] != "connected" or device_id not in AGENT_CONNECTIONS:
            _agent_audit(connection, customer["id"], "command_rejected_disconnected", device_id=device_id)
            connection.commit()
            raise HTTPException(status_code=409, detail="Device is not connected")

        permissions = json.loads(device["permissions"])
        if request.action not in permissions:
            _agent_audit(
                connection,
                customer["id"],
                "command_rejected_permission",
                device_id=device_id,
                details={"action": request.action},
            )
            connection.commit()
            raise HTTPException(status_code=403, detail="Device does not permit this action")

        now = _utc_now()
        cutoff = _utc_text(now - timedelta(minutes=1))
        count = connection.execute(
            """
            SELECT COUNT(*) FROM agent_rate_events
            WHERE owner_id = ? AND created_at >= ?
            """,
            (customer["id"], cutoff),
        ).fetchone()[0]
        if count >= AGENT_REQUESTS_PER_MINUTE:
            _agent_audit(connection, customer["id"], "command_rate_limited", device_id=device_id)
            connection.commit()
            raise HTTPException(status_code=429, detail="Command rate limit exceeded")
        connection.execute(
            "DELETE FROM agent_rate_events WHERE created_at < ?",
            (cutoff,),
        )

        command_id = str(uuid.uuid4())
        approval_id = str(uuid.uuid4())
        approval_token = secrets.token_urlsafe(32)
        expires_at = _utc_text(now + timedelta(seconds=AGENT_APPROVAL_TTL_SECONDS))
        try:
            connection.execute(
                """
                INSERT INTO agent_commands
                (id, owner_id, device_id, client_request_id, action, payload,
                 status, created_at, updated_at, deadline_at)
                VALUES (?, ?, ?, ?, ?, ?, 'awaiting_approval', ?, ?, ?)
                """,
                (
                    command_id,
                    customer["id"],
                    device_id,
                    request.client_request_id,
                    request.action,
                    json.dumps(request.payload, separators=(",", ":"), sort_keys=True),
                    _utc_text(now),
                    _utc_text(now),
                    expires_at,
                ),
            )
        except sqlite3.IntegrityError:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Request ID has already been used")
        connection.execute(
            """
            INSERT INTO agent_approvals
            (id, owner_id, command_id, token_hash, status, expires_at, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                approval_id,
                customer["id"],
                command_id,
                hashlib.sha256(approval_token.encode("utf-8")).hexdigest(),
                expires_at,
                _utc_text(now),
            ),
        )
        connection.execute(
            "INSERT INTO agent_rate_events (owner_id, created_at) VALUES (?, ?)",
            (customer["id"], _utc_text(now)),
        )
        _agent_audit(
            connection,
            customer["id"],
            "command_requested",
            device_id=device_id,
            command_id=command_id,
            details={"action": request.action},
        )
        connection.commit()
    finally:
        connection.close()

    return {
        "command_id": command_id,
        "status": "awaiting_approval",
        "approval": {
            "request_id": approval_id,
            "expires_at": expires_at,
            "approval_token": approval_token,
        },
        "execution_enabled": DESKTOP_EXECUTION_ENABLED,
    }


@app.get("/computer/agent/approvals")
def list_agent_approvals(customer: dict = Depends(get_current_customer)):
    connection = get_db()
    try:
        with connection:
            _expire_agent_approvals(connection, customer["id"])
        rows = connection.execute(
            """
            SELECT a.id AS request_id, a.command_id, a.expires_at, a.created_at,
                   c.device_id, c.action, c.payload
            FROM agent_approvals a
            JOIN agent_commands c ON c.id = a.command_id
            WHERE a.owner_id = ? AND a.status = 'pending'
            ORDER BY a.created_at ASC
            """,
            (customer["id"],),
        ).fetchall()
    finally:
        connection.close()

    return {
        "approvals": [
            {
                **dict(row),
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]
    }


@app.post("/computer/agent/approvals/{request_id}/deny")
def deny_agent_approval(
    request_id: str,
    decision: AgentApprovalDecision,
    customer: dict = Depends(get_current_customer),
):
    supplied_hash = hashlib.sha256(decision.approval_token.encode("utf-8")).hexdigest()
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        approval = connection.execute(
            """
            SELECT id, command_id, token_hash, expires_at FROM agent_approvals
            WHERE id = ? AND owner_id = ? AND status = 'pending'
            """,
            (request_id, customer["id"]),
        ).fetchone()
        if approval is None:
            connection.rollback()
            raise HTTPException(status_code=404, detail="Approval request not found")
        if not hmac.compare_digest(supplied_hash, approval["token_hash"]):
            connection.rollback()
            raise HTTPException(status_code=403, detail="Invalid approval token")
        now = _utc_text()
        if approval["expires_at"] <= now:
            connection.execute(
                "UPDATE agent_approvals SET status = 'expired' WHERE id = ?",
                (request_id,),
            )
            connection.execute(
                """
                UPDATE agent_commands SET status = 'expired', updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (now, approval["command_id"], customer["id"]),
            )
            connection.commit()
            raise HTTPException(status_code=409, detail="Approval request has expired")
        connection.execute(
            """
            UPDATE agent_approvals SET status = 'denied', consumed_at = ?
            WHERE id = ? AND owner_id = ? AND status = 'pending'
            """,
            (now, request_id, customer["id"]),
        )
        connection.execute(
            """
            UPDATE agent_commands SET status = 'denied', updated_at = ?
            WHERE id = ? AND owner_id = ? AND status = 'awaiting_approval'
            """,
            (now, approval["command_id"], customer["id"]),
        )
        _agent_audit(
            connection,
            customer["id"],
            "command_denied",
            command_id=approval["command_id"],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {"request_id": request_id, "command_id": approval["command_id"], "status": "denied"}


@app.post("/computer/agent/approvals/{request_id}/approve")
async def approve_agent_command(
    request_id: str,
    decision: AgentApprovalDecision,
    customer: dict = Depends(get_current_customer),
):
    supplied_hash = hashlib.sha256(decision.approval_token.encode("utf-8")).hexdigest()
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        approval = connection.execute(
            """
            SELECT a.id, a.command_id, a.token_hash, a.expires_at,
                   c.device_id, c.action, c.payload
            FROM agent_approvals a
            JOIN agent_commands c ON c.id = a.command_id
            WHERE a.id = ? AND a.owner_id = ? AND a.status = 'pending'
              AND c.owner_id = ?
            """,
            (request_id, customer["id"], customer["id"]),
        ).fetchone()
        if approval is None:
            connection.rollback()
            raise HTTPException(status_code=404, detail="Approval request not found")
        if not hmac.compare_digest(supplied_hash, approval["token_hash"]):
            connection.rollback()
            raise HTTPException(status_code=403, detail="Invalid approval token")
        now = _utc_text()
        if approval["expires_at"] <= now:
            connection.execute(
                "UPDATE agent_approvals SET status = 'expired' WHERE id = ?",
                (request_id,),
            )
            connection.execute(
                """
                UPDATE agent_commands SET status = 'expired', updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (now, approval["command_id"], customer["id"]),
            )
            connection.commit()
            raise HTTPException(status_code=409, detail="Approval request has expired")

        device = connection.execute(
            """
            SELECT status, permissions FROM agent_devices
            WHERE id = ? AND owner_id = ?
            """,
            (approval["device_id"], customer["id"]),
        ).fetchone()
        if (
            device is None
            or device["status"] != "connected"
            or approval["device_id"] not in AGENT_CONNECTIONS
        ):
            connection.rollback()
            raise HTTPException(status_code=409, detail="Device is not connected")
        if _agent_safety_stopped(connection, customer["id"]):
            connection.rollback()
            raise HTTPException(status_code=423, detail="Emergency stop is active")
        if approval["action"] not in json.loads(device["permissions"]):
            connection.rollback()
            raise HTTPException(status_code=403, detail="Device no longer permits this action")

        deadline = _utc_text(_utc_now() + timedelta(seconds=AGENT_COMMAND_TIMEOUT_SECONDS))
        connection.execute(
            """
            UPDATE agent_approvals SET status = 'approved', consumed_at = ?
            WHERE id = ? AND owner_id = ? AND status = 'pending'
            """,
            (now, request_id, customer["id"]),
        )
        connection.execute(
            """
            UPDATE agent_commands SET status = 'dispatched', updated_at = ?, deadline_at = ?
            WHERE id = ? AND owner_id = ? AND status = 'awaiting_approval'
            """,
            (now, deadline, approval["command_id"], customer["id"]),
        )
        _agent_audit(
            connection,
            customer["id"],
            "command_approved_and_dispatched",
            device_id=approval["device_id"],
            command_id=approval["command_id"],
            details={"execution_enabled": DESKTOP_EXECUTION_ENABLED},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    command = {
        "type": "command",
        "command_id": approval["command_id"],
        "action": approval["action"],
        "payload": json.loads(approval["payload"]),
        "execution_enabled": DESKTOP_EXECUTION_ENABLED,
    }
    try:
        validate_command(command)
    except ValueError:
        _fail_agent_command(approval["command_id"], customer["id"], "Command validation failed")
        raise HTTPException(status_code=422, detail="Command failed Local Agent validation")

    websocket = AGENT_CONNECTIONS.get(approval["device_id"])
    if websocket is None:
        _fail_agent_command(approval["command_id"], customer["id"], "Device disconnected before dispatch")
        raise HTTPException(status_code=409, detail="Device disconnected before dispatch")

    loop = asyncio.get_running_loop()
    response_future = loop.create_future()
    PENDING_AGENT_COMMANDS[approval["command_id"]] = response_future
    try:
        await websocket.send_json(command)
        result = await asyncio.wait_for(
            response_future,
            timeout=AGENT_COMMAND_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        _fail_agent_command(
            approval["command_id"],
            customer["id"],
            "Approval request was interrupted",
        )
        raise
    except asyncio.TimeoutError:
        _fail_agent_command(approval["command_id"], customer["id"], "Local Agent response timed out")
        result = {
            "command_id": approval["command_id"],
            "action": approval["action"],
            "success": False,
            "message": "Local Agent response timed out",
        }
    except Exception:
        _fail_agent_command(approval["command_id"], customer["id"], "Local Agent dispatch failed")
        result = {
            "command_id": approval["command_id"],
            "action": approval["action"],
            "success": False,
            "message": "Local Agent dispatch failed",
        }
    finally:
        PENDING_AGENT_COMMANDS.pop(approval["command_id"], None)

    return {
        "request_id": request_id,
        "command_id": approval["command_id"],
        "status": "failed" if not result["success"] else "completed",
        "result": result,
        "execution_enabled": DESKTOP_EXECUTION_ENABLED,
    }


def _fail_agent_command(command_id, owner_id, message):
    connection = get_db()
    try:
        with connection:
            row = connection.execute(
                """
                SELECT device_id, action FROM agent_commands
                WHERE id = ? AND owner_id = ? AND status = 'dispatched'
                """,
                (command_id, owner_id),
            ).fetchone()
            if row is None:
                return
            result = {
                "command_id": command_id,
                "action": row["action"],
                "success": False,
                "message": message,
            }
            connection.execute(
                """
                UPDATE agent_commands SET status = 'failed', result = ?, updated_at = ?
                WHERE id = ? AND owner_id = ? AND status = 'dispatched'
                """,
                (json.dumps(result), _utc_text(), command_id, owner_id),
            )
            _agent_audit(
                connection,
                owner_id,
                "command_failed",
                device_id=row["device_id"],
                command_id=command_id,
                details={"reason": message},
            )
    finally:
        connection.close()


async def _record_agent_result(device_id, incoming):
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        command = connection.execute(
            """
            SELECT id, owner_id, action, deadline_at
            FROM agent_commands
            WHERE id = ? AND device_id = ? AND status = 'dispatched'
            """,
            (incoming.command_id, device_id),
        ).fetchone()
        if command is None or command["action"] != incoming.action or command["deadline_at"] <= _utc_text():
            device = connection.execute(
                "SELECT owner_id FROM agent_devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            if device is not None:
                _agent_audit(
                    connection,
                    device["owner_id"],
                    "agent_response_rejected",
                    device_id=device_id,
                    command_id=incoming.command_id,
                    details={"reason": "unsolicited, mismatched, or expired response"},
                )
                connection.commit()
            else:
                connection.rollback()
            return False

        device = connection.execute(
            """
            SELECT status FROM agent_devices
            WHERE id = ? AND owner_id = ?
            """,
            (device_id, command["owner_id"]),
        ).fetchone()
        if (
            device is None
            or device["status"] != "connected"
            or _agent_safety_stopped(connection, command["owner_id"])
        ):
            _agent_audit(
                connection,
                command["owner_id"],
                "agent_response_rejected",
                device_id=device_id,
                command_id=incoming.command_id,
                details={"reason": "device revoked, disconnected, or emergency stop active"},
            )
            connection.commit()
            return False

        result = {
            "command_id": incoming.command_id,
            "action": incoming.action,
            "success": False,
            "message": "Desktop execution is disabled; Local Agent result was not accepted as execution.",
        }
        connection.execute(
            """
            UPDATE agent_commands SET status = 'failed', result = ?, updated_at = ?
            WHERE id = ? AND owner_id = ? AND device_id = ? AND status = 'dispatched'
            """,
            (
                json.dumps(result),
                _utc_text(),
                incoming.command_id,
                command["owner_id"],
                device_id,
            ),
        )
        _agent_audit(
            connection,
            command["owner_id"],
            "agent_response_recorded_execution_disabled",
            device_id=device_id,
            command_id=incoming.command_id,
            details={"agent_claimed_success": incoming.success},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    future = PENDING_AGENT_COMMANDS.get(incoming.command_id)
    if future is not None and not future.done():
        future.set_result(result)
    return True


@app.websocket("/computer/agent/ws/{device_id}")
async def local_agent_websocket(websocket: WebSocket, device_id: str):
    await websocket.accept()
    owner_id = None
    try:
        mode = os.getenv("NEX_ENV", "production").strip().lower()
        if mode != "development" and websocket.url.scheme != "wss":
            await websocket.close(code=1008, reason="Secure WebSocket is required")
            return
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if (
            not isinstance(auth_message, dict)
            or set(auth_message) != {"type", "credential"}
            or auth_message.get("type") != "authenticate"
            or not isinstance(auth_message.get("credential"), str)
        ):
            await websocket.close(code=1008, reason="Invalid authentication message")
            return

        connection = get_db()
        try:
            device = connection.execute(
                """
                SELECT owner_id, credential_hash, status
                FROM agent_devices WHERE id = ?
                """,
                (device_id,),
            ).fetchone()
            if device is None:
                await websocket.close(code=1008, reason="Unknown device")
                return
            owner_id = device["owner_id"]
            supplied_hash = hashlib.sha256(auth_message["credential"].encode("utf-8")).hexdigest()
            if (
                not hmac.compare_digest(supplied_hash, device["credential_hash"])
                or device["status"] == "revoked"
                or _agent_safety_stopped(connection, owner_id)
            ):
                _agent_audit(
                    connection,
                    owner_id,
                    "agent_authentication_rejected",
                    device_id=device_id,
                )
                connection.commit()
                await websocket.close(code=1008, reason="Device authentication failed")
                return
            with connection:
                connection.execute(
                    """
                    UPDATE agent_devices SET status = 'connected', last_seen_at = ?
                    WHERE id = ? AND owner_id = ? AND status != 'revoked'
                    """,
                    (_utc_text(), device_id, owner_id),
                )
                _agent_audit(connection, owner_id, "agent_connected", device_id=device_id)
        finally:
            connection.close()

        previous_connection = AGENT_CONNECTIONS.get(device_id)
        AGENT_CONNECTIONS[device_id] = websocket
        if previous_connection is not None:
            await previous_connection.close(code=1008, reason="Replaced by a new connection")
        await websocket.send_json({"type": "authenticated", "device_id": device_id})
        while True:
            raw_result = await websocket.receive_json()
            try:
                incoming = AgentCommandResult.parse_obj(raw_result)
            except ValidationError:
                connection = get_db()
                try:
                    with connection:
                        _agent_audit(
                            connection,
                            owner_id,
                            "agent_response_rejected",
                            device_id=device_id,
                            details={"reason": "invalid result payload"},
                        )
                finally:
                    connection.close()
                await websocket.close(code=1008, reason="Invalid command result")
                return
            if not await _record_agent_result(device_id, incoming):
                await websocket.close(code=1008, reason="Unsolicited or invalid command result")
                return
            await websocket.send_json({
                "type": "result_ack",
                "command_id": incoming.command_id,
                "accepted": True,
            })
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        if AGENT_CONNECTIONS.get(device_id) is websocket:
            AGENT_CONNECTIONS.pop(device_id, None)
            connection = get_db()
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE agent_devices SET status = 'disconnected'
                        WHERE id = ? AND owner_id = ? AND status = 'connected'
                        """,
                        (device_id, owner_id),
                    )
                    if owner_id:
                        _agent_audit(connection, owner_id, "agent_disconnected", device_id=device_id)
            finally:
                connection.close()


@app.get("/computer/agent/commands")
def list_agent_commands(customer: dict = Depends(get_current_customer)):
    connection = get_db()
    try:
        rows = connection.execute(
            """
            SELECT id AS command_id, device_id, action, payload, status,
                   result, created_at, updated_at
            FROM agent_commands WHERE owner_id = ?
            ORDER BY created_at DESC LIMIT 100
            """,
            (customer["id"],),
        ).fetchall()
    finally:
        connection.close()
    return {
        "commands": [
            {
                **dict(row),
                "payload": json.loads(row["payload"]),
                "result": json.loads(row["result"]) if row["result"] else None,
            }
            for row in rows
        ]
    }


@app.get("/computer/agent/commands/{command_id}")
def get_agent_command(
    command_id: str,
    customer: dict = Depends(get_current_customer),
):
    connection = get_db()
    try:
        row = connection.execute(
            """
            SELECT id AS command_id, device_id, action, payload, status,
                   result, created_at, updated_at, deadline_at
            FROM agent_commands WHERE id = ? AND owner_id = ?
            """,
            (command_id, customer["id"]),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Command not found")
    return {
        **dict(row),
        "payload": json.loads(row["payload"]),
        "result": json.loads(row["result"]) if row["result"] else None,
        "execution_enabled": DESKTOP_EXECUTION_ENABLED,
    }
