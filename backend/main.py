from source_validator import validate_sources
from agent_executor import execute_agent, generate_plan, extract_handoff_context
from research_report import build_research_task_plan
from self_healing import analyze_failure, should_retry, execute_with_retries
from auth import hash_password, verify_password, create_access_token, decode_access_token
from migrations import apply_migrations, payload_hash
from local_agent_protocol import ALLOWED_ACTIONS as LOCAL_AGENT_ALLOWED_ACTIONS
from local_agent_protocol import MAX_MESSAGE_BYTES, parse_message, validate_command
from desktop_runtime import desktop_execution_enabled
from browser_runtime import browser_automation_enabled
from browser_security import configured_browser_domains, validate_browser_url
from vision_engine import MAX_SCREENSHOT_BYTES, VisionEngine
from computer_planner import (
    MIN_ACTION_CONFIDENCE,
    OPERATION_ACTIONS,
    SOURCE_OPERATIONS,
    build_action_proposal,
    parse_agent_candidate,
)
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
from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr, ValidationError
from typing import Optional, List, Dict, Any, Literal
import sqlite3
import uuid
import json
import re
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
    device_observation_upload = (
        request.method == "POST"
        and re.fullmatch(
            r"/computer/agent/devices/[^/]+/commands/[^/]+/observation",
            request.url.path,
        )
    )

    mode = os.getenv("NEX_ENV", "production").strip().lower()
    if mode not in {"development", "production"}:
        return JSONResponse(
            status_code=503,
            content={"detail": "Invalid server environment configuration"},
        )
    if device_observation_upload:
        return await call_next(request)

    if mode == "development":
        limited = _agent_http_rate_limit_response(request)
        if limited is not None:
            return limited
        return await call_next(request)

    api_key = os.getenv("NEX_API_KEY", "")
    if len(api_key.encode("utf-8")) < 32:
        return JSONResponse(
            status_code=503,
            content={"detail": "API authentication is not securely configured"},
        )

    provided_key = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(provided_key, api_key):
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid API key"},
        )

    limited = _agent_http_rate_limit_response(request)
    if limited is not None:
        return limited
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
    connection.execute("PRAGMA foreign_keys = ON")
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
    apply_migrations(connection)
    startup_time = datetime.now(timezone.utc).isoformat()
    stale_commands = connection.execute(
        """
        SELECT id, owner_id, device_id, action FROM agent_commands
        WHERE status IN ('queued', 'executing')
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
            WHERE id = ? AND status IN ('queued', 'executing')
            """,
            (json.dumps(result), startup_time, row["id"]),
        )
        connection.execute(
            """
            UPDATE agent_execution_sessions
            SET state = 'failed', ended_at = ?
            WHERE owner_id = ? AND command_id = ?
            """,
            (startup_time, row["owner_id"], row["id"]),
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
    connected_devices = connection.execute(
        "SELECT id, owner_id FROM agent_devices WHERE status = 'connected'"
    ).fetchall()
    connection.execute(
        "UPDATE agent_devices SET status = 'disconnected' WHERE status = 'connected'"
    )
    for device in connected_devices:
        _agent_audit(
            connection,
            device["owner_id"],
            "agent_disconnected",
            device_id=device["id"],
            details={"reason": "backend restarted"},
        )

    connection.commit()
    connection.close()


init_db()


DESKTOP_EXECUTION_ENABLED = desktop_execution_enabled()
BROWSER_AUTOMATION_ENABLED = browser_automation_enabled()
AGENT_COMMAND_TIMEOUT_SECONDS = 15
AGENT_BROWSER_TIMEOUT_SECONDS = 25
AGENT_SCREEN_ANALYSIS_TIMEOUT_SECONDS = 35
AGENT_APPROVAL_TTL_SECONDS = 300
OBSERVATION_TTL_SECONDS = 900
ACTION_PROPOSAL_TTL_SECONDS = 300
AGENT_REQUESTS_PER_MINUTE = 20
AGENT_PAIRINGS_PER_HOUR = 5
AGENT_APPROVAL_ATTEMPTS_PER_MINUTE = 10
AGENT_WS_MESSAGES_PER_MINUTE = 120
AGENT_WS_AUTH_ATTEMPTS_PER_MINUTE = 10
AGENT_HEARTBEAT_TIMEOUT_SECONDS = 45
ALLOWED_AGENT_ACTIONS = set(LOCAL_AGENT_ALLOWED_ACTIONS)
AGENT_APP_ALLOWLIST = LOCAL_AGENT_ALLOWED_ACTIONS["open_app"]["app_id"]
AGENT_CONNECTIONS = {}
AGENT_DISPATCH_LOCK = asyncio.Lock()
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
    permissions: List[str] = Field(default_factory=list, max_length=14)

    class Config:
        extra = "forbid"


class AgentCommandRequest(BaseModel):
    action: Literal[
        "open_app",
        "capture_screen",
        "get_active_window",
        "input_text",
        "read_file",
        "write_file",
        "click",
        "browser_navigate",
        "browser_read_page",
        "browser_inspect_controls",
        "browser_fill",
        "browser_submit",
        "browser_download",
        "browser_close",
    ]
    payload: Dict[str, Any] = Field(default_factory=dict)
    client_request_id: str = Field(min_length=16, max_length=128)
    proposal_id: Optional[StrictStr] = Field(default=None, min_length=36, max_length=36)

    class Config:
        extra = "forbid"


class ComputerActionProposalRequest(BaseModel):
    instruction: StrictStr = Field(min_length=1, max_length=1000)
    operation: Literal[
        "desktop.click",
        "browser.navigate",
        "browser.read_page",
        "browser.inspect_controls",
        "browser.fill",
        "browser.submit",
        "browser.download",
        "browser.close",
    ]
    observation_id: Optional[StrictStr] = Field(default=None, max_length=64)
    browser_session_id: Optional[StrictStr] = Field(default=None, max_length=64)
    target_element_id: Optional[StrictStr] = Field(default=None, max_length=64)
    url: Optional[StrictStr] = Field(default=None, max_length=2048)
    text: Optional[StrictStr] = Field(default=None, max_length=2000)
    expected_result: Optional[Dict[str, Any]] = None

    class Config:
        extra = "forbid"


class ComputerActionDraftRequest(BaseModel):
    instruction: StrictStr = Field(min_length=1, max_length=1000)
    observation_id: StrictStr = Field(min_length=36, max_length=64)
    browser_session_id: Optional[StrictStr] = Field(default=None, max_length=64)
    text: Optional[StrictStr] = Field(default=None, max_length=2000)
    expected_result: Optional[Dict[str, Any]] = None

    class Config:
        extra = "forbid"


class ComputerActionVerificationRequest(BaseModel):
    observation_id: StrictStr = Field(min_length=36, max_length=64)

    class Config:
        extra = "forbid"


class AgentApprovalDecision(BaseModel):
    approval_token: StrictStr = Field(min_length=32, max_length=128)

    class Config:
        extra = "forbid"


class AgentCommandResult(BaseModel):
    command_id: StrictStr = Field(min_length=1, max_length=64)
    action: Literal[
        "open_app",
        "capture_screen",
        "get_active_window",
        "input_text",
        "read_file",
        "write_file",
        "click",
        "browser_navigate",
        "browser_read_page",
        "browser_inspect_controls",
        "browser_fill",
        "browser_submit",
        "browser_download",
        "browser_close",
    ]
    success: StrictBool
    message: StrictStr = Field(min_length=1, max_length=500)
    session_id: Optional[StrictStr] = Field(default=None, max_length=64)
    observation_id: Optional[StrictStr] = Field(default=None, max_length=64)
    data: Optional[Dict[str, Any]] = None
    observation: Optional[Dict[str, Any]] = None
    page_identity: Optional[StrictStr] = Field(default=None, min_length=36, max_length=36)
    navigation_generation: Optional[StrictInt] = Field(default=None, ge=1)
    session_closed: Optional[StrictBool] = None
    failure_code: Optional[Literal["desktop_click_stale"]] = None
    verification_status: Optional[Literal["verified", "observed", "unverified"]] = None

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
        name="Planner",
        role="Planning AI",
        goal="Break customer goals into bounded tasks and dependencies"
    ),
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
        name="Computer Agent",
        role="Approved Computer Use AI",
        goal="Propose computer actions from observations without bypassing approval"
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


def _consume_agent_rate_limit(connection, owner_id, scope, limit, window_seconds=60):
    now = _utc_now()
    cutoff = _utc_text(now - timedelta(seconds=window_seconds))
    connection.execute(
        """
        DELETE FROM agent_rate_events WHERE created_at < ?
        """,
        (_utc_text(now - timedelta(hours=1)),),
    )
    connection.execute(
        """
        DELETE FROM agent_rate_events
        WHERE owner_id = ? AND scope = ? AND created_at < ?
        """,
        (owner_id, scope, cutoff),
    )
    count = connection.execute(
        """
        SELECT COUNT(*) FROM agent_rate_events
        WHERE owner_id = ? AND scope = ? AND created_at >= ?
        """,
        (owner_id, scope, cutoff),
    ).fetchone()[0]
    if count >= limit:
        return False
    connection.execute(
        "INSERT INTO agent_rate_events (owner_id, scope, created_at) VALUES (?, ?, ?)",
        (owner_id, scope, _utc_text(now)),
    )
    return True


def _agent_http_rate_limit_response(request):
    if request.method != "POST":
        return None
    path = request.url.path.strip("/").split("/")
    if path == ["computer", "agent", "devices"]:
        scope, limit, window = "pairing", AGENT_PAIRINGS_PER_HOUR, 3600
    elif (
        len(path) == 5
        and path[:3] == ["computer", "agent", "devices"]
        and path[4] == "commands"
    ):
        scope, limit, window = "commands", AGENT_REQUESTS_PER_MINUTE, 60
    elif (
        len(path) == 5
        and path[:3] == ["computer", "agent", "approvals"]
        and path[4] in {"approve", "deny"}
    ):
        scope, limit, window = "approval", AGENT_APPROVAL_ATTEMPTS_PER_MINUTE, 60
    else:
        return None

    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return None
    try:
        owner_id = decode_access_token(token)
    except (jwt.InvalidTokenError, RuntimeError):
        return None

    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        customer = connection.execute(
            "SELECT id FROM customers WHERE id = ? AND is_active = 1",
            (owner_id,),
        ).fetchone()
        if customer is None:
            connection.rollback()
            return None
        if not _consume_agent_rate_limit(connection, owner_id, scope, limit, window):
            _agent_audit(
                connection,
                owner_id,
                "rate_limit_exceeded",
                details={"scope": scope},
            )
            connection.commit()
            return JSONResponse(
                status_code=429,
                content={"detail": f"{scope.capitalize()} rate limit exceeded"},
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return None


def _agent_safety_stopped(connection, owner_id):
    row = connection.execute(
        "SELECT emergency_stopped FROM agent_safety WHERE owner_id = ?",
        (owner_id,),
    ).fetchone()
    return bool(row["emergency_stopped"]) if row else False


def _validate_agent_action(action, payload):
    if action not in ALLOWED_AGENT_ACTIONS or not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Action or payload is invalid")
    try:
        validate_command(
            {
                "type": "command",
                "command_id": "validation-command",
                "action": action,
                "payload": payload,
                "execution_enabled": True,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _action_execution_enabled(action):
    return BROWSER_AUTOMATION_ENABLED if action.startswith("browser_") else DESKTOP_EXECUTION_ENABLED


def _cleanup_expired_computer_data(connection, owner_id):
    now = _utc_text()
    connection.execute(
        """
        UPDATE computer_action_proposals
        SET status = CASE
                WHEN status IN ('proposed', 'pending_approval') THEN 'expired'
                ELSE status
            END,
            observation_id = NULL,
            browser_session_id = NULL,
            target_element_id = NULL,
            payload = '{}',
            expected_result = '{}',
            verification_status = CASE
                WHEN verification_status = 'pending' THEN 'failed'
                ELSE verification_status
            END,
            updated_at = ?
        WHERE owner_id = ? AND expires_at <= ?
        """,
        (now, owner_id, now),
    )
    connection.execute(
        "DELETE FROM computer_observations WHERE owner_id = ? AND expires_at <= ?",
        (owner_id, now),
    )
    connection.execute(
        """
        UPDATE browser_sessions SET status = 'expired', initial_url = NULL
        WHERE owner_id = ? AND expires_at <= ? AND status != 'closed'
        """,
        (owner_id, now),
    )


def _store_observation(
    connection,
    owner_id,
    device_id,
    source,
    width,
    height,
    provider,
    elements,
    command_id=None,
    image_sha256=None,
    observation_id=None,
    browser_session_id=None,
    page_identity=None,
    navigation_generation=None,
    active_window_identity=None,
):
    observation_id = observation_id or str(uuid.uuid4())
    captured_at = _utc_text()
    expires_at = _utc_text(_utc_now() + timedelta(seconds=OBSERVATION_TTL_SECONDS))
    connection.execute(
        """
        INSERT INTO computer_observations
        (id, owner_id, device_id, source, command_id, browser_session_id,
         page_identity, navigation_generation, screen_width, screen_height,
         provider, image_sha256, active_window_identity, elements, captured_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            owner_id,
            device_id,
            source,
            command_id,
            browser_session_id,
            page_identity,
            navigation_generation,
            width,
            height,
            provider,
            image_sha256,
            active_window_identity,
            json.dumps(elements, separators=(",", ":"), sort_keys=True),
            captured_at,
            expires_at,
        ),
    )
    return {
        "observation_id": observation_id,
        "device_id": device_id,
        "source": source,
        "browser_session_id": browser_session_id,
        "page_identity": page_identity,
        "navigation_generation": navigation_generation,
        "screen_dimensions": {"width": width, "height": height},
        "image_sha256": image_sha256,
        "active_window_identity": active_window_identity,
        "elements": elements,
        "timestamp": captured_at,
        "expires_at": expires_at,
    }


def _validate_dom_observation(observation):
    if not isinstance(observation, dict) or set(observation) != {
        "source", "screen_width", "screen_height", "elements",
        "browser_session_id", "page_identity", "navigation_generation",
    }:
        raise ValueError("Browser observation schema is invalid")
    width = observation["screen_width"]
    height = observation["screen_height"]
    elements = observation["elements"]
    if (
        observation["source"] != "browser_dom"
        or isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
        or not 1 <= width <= 4096
        or not 1 <= height <= 4096
        or not isinstance(elements, list)
        or len(elements) > 20
        or not isinstance(observation["browser_session_id"], str)
        or not re.fullmatch(r"[a-f0-9-]{36}", observation["browser_session_id"])
        or not isinstance(observation["page_identity"], str)
        or not re.fullmatch(r"[a-f0-9-]{36}", observation["page_identity"])
        or isinstance(observation["navigation_generation"], bool)
        or not isinstance(observation["navigation_generation"], int)
        or observation["navigation_generation"] < 1
    ):
        raise ValueError("Browser observation bounds are invalid")
    required = {
        "element_id", "type", "description", "bounding_box",
        "confidence", "ambiguous", "control",
    }
    for element in elements:
        if (
            not isinstance(element, dict)
            or set(element) != required
            or not isinstance(element["element_id"], str)
            or not isinstance(element["description"], str)
            or len(element["description"]) > 200
            or element["type"] not in {"button", "link", "text_field"}
            or element["confidence"] != 1.0
            or element["ambiguous"] is not False
            or not isinstance(element["control"], dict)
        ):
            raise ValueError("Browser observation contains an invalid control")
        box = element["bounding_box"]
        if (
            not isinstance(box, dict)
            or set(box) != {"x", "y", "width", "height"}
            or any(
                isinstance(box.get(key), bool)
                or not isinstance(box.get(key), (int, float))
                for key in ("x", "y", "width", "height")
            )
            or box["x"] < 0
            or box["y"] < 0
            or box["width"] <= 0
            or box["height"] <= 0
            or box["x"] + box["width"] > width
            or box["y"] + box["height"] > height
        ):
            raise ValueError("Browser observation contains out-of-bounds geometry")
    return (
        width,
        height,
        elements,
        observation["browser_session_id"],
        observation["page_identity"],
        observation["navigation_generation"],
    )


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
            UPDATE agent_approvals SET status = 'expired', consumed_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            [(now, row["id"]) for row in expired],
        )
        connection.executemany(
            """
            UPDATE agent_commands
            SET status = 'expired', updated_at = ?
            WHERE id = ? AND status = 'pending_approval'
            """,
            [(now, row["command_id"]) for row in expired],
        )
        connection.executemany(
            """
            UPDATE computer_action_proposals
            SET status = 'expired', updated_at = ?
            WHERE id = (
                SELECT proposal_id FROM agent_commands WHERE id = ?
            ) AND status = 'pending_approval'
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
        connection.execute("BEGIN IMMEDIATE")
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
            )
        )
        _agent_audit(
            connection,
            customer["id"],
            "device_paired",
            device_id=device_id,
            details={"permissions": request.permissions},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
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
    async with AGENT_DISPATCH_LOCK:
        return await _revoke_agent_device_locked(device_id, customer)


async def _revoke_agent_device_locked(device_id, customer):
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _cleanup_expired_computer_data(connection, customer["id"])
        device = connection.execute(
            "SELECT id FROM agent_devices WHERE id = ? AND owner_id = ?",
            (device_id, customer["id"]),
        ).fetchone()
        if device is None:
            connection.rollback()
            raise HTTPException(status_code=404, detail="Device not found")
        cancelled_commands = connection.execute(
            """
            SELECT id, action FROM agent_commands
            WHERE owner_id = ? AND device_id = ?
              AND status IN ('pending_approval', 'approved', 'queued', 'executing')
            """,
            (customer["id"], device_id),
        ).fetchall()
        now = _utc_text()
        connection.execute(
            "UPDATE agent_devices SET status = 'revoked' WHERE id = ? AND owner_id = ?",
            (device_id, customer["id"]),
        )
        connection.execute(
            """
            UPDATE agent_approvals SET status = 'cancelled', consumed_at = ?
            WHERE owner_id = ? AND status = 'pending'
              AND command_id IN (
                  SELECT id FROM agent_commands
                  WHERE device_id = ? AND status = 'pending_approval'
              )
            """,
            (now, customer["id"], device_id),
        )
        for command in cancelled_commands:
            result = {
                "command_id": command["id"],
                "action": command["action"],
                "success": False,
                "message": "Device was revoked before the command completed",
            }
            connection.execute(
                """
                UPDATE agent_commands
                SET status = 'cancelled', result = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending_approval', 'approved', 'queued', 'executing')
                """,
                (json.dumps(result), now, command["id"]),
            )
            connection.execute(
                """
                UPDATE agent_execution_sessions
                SET state = 'cancelled', ended_at = ?
                WHERE owner_id = ? AND command_id = ?
                """,
                (now, customer["id"], command["id"]),
            )
            connection.execute(
                """
                UPDATE agent_execution_sessions
                SET state = 'cancelled', ended_at = ?
                WHERE owner_id = ? AND command_id = ?
                """,
                (now, customer["id"], command["id"]),
            )
            _agent_audit(
                connection,
                customer["id"],
                "command_cancelled_device_revoked",
                device_id=device_id,
                command_id=command["id"],
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
    async with AGENT_DISPATCH_LOCK:
        return await _emergency_stop_locked(customer)


async def _emergency_stop_locked(customer):
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cancelled_commands = connection.execute(
            """
            SELECT id, device_id, action FROM agent_commands
            WHERE owner_id = ? AND status IN ('pending_approval', 'approved', 'queued', 'executing')
            """,
            (customer["id"],),
        ).fetchall()
        now = _utc_text()
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
            SET status = 'cancelled', consumed_at = ?
            WHERE owner_id = ? AND status = 'pending'
            """,
            (now, customer["id"]),
        )
        for command in cancelled_commands:
            result = {
                "command_id": command["id"],
                "action": command["action"],
                "success": False,
                "message": "Emergency stop activated before the command completed",
            }
            connection.execute(
                """
                UPDATE agent_commands
                SET status = 'cancelled', result = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending_approval', 'approved', 'queued', 'executing')
                """,
                (json.dumps(result), now, command["id"]),
            )
            _agent_audit(
                connection,
                customer["id"],
                "command_cancelled_emergency_stop",
                device_id=command["device_id"],
                command_id=command["id"],
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


@app.post("/computer/agent/devices/{device_id}/proposals", status_code=201)
def create_computer_action_proposal(
    device_id: str,
    request: ComputerActionProposalRequest,
    customer: dict = Depends(get_current_customer),
):
    action, permission, observation_source, _element_type = OPERATION_ACTIONS[request.operation]
    if action.startswith("browser_") and not BROWSER_AUTOMATION_ENABLED:
        raise HTTPException(status_code=409, detail="Browser automation is disabled")
    if action == "click" and not DESKTOP_EXECUTION_ENABLED:
        raise HTTPException(status_code=409, detail="Desktop execution is disabled")

    supplied_fields = {
        key
        for key, value in (
            request.model_dump() if hasattr(request, "model_dump") else request.dict()
        ).items()
        if key not in {"operation", "instruction"} and value is not None
    }
    allowed_fields = {
        "desktop.click": {
            "observation_id", "target_element_id", "expected_result"
        },
        "browser.navigate": {"browser_session_id", "url"},
        "browser.read_page": {"browser_session_id"},
        "browser.inspect_controls": {"browser_session_id"},
        "browser.fill": {
            "observation_id", "browser_session_id", "target_element_id", "text"
        },
        "browser.submit": {
            "observation_id", "browser_session_id", "target_element_id", "expected_result"
        },
        "browser.download": {
            "observation_id", "browser_session_id", "target_element_id"
        },
        "browser.close": {"browser_session_id"},
    }[request.operation]
    if supplied_fields - allowed_fields:
        raise HTTPException(status_code=422, detail="Proposal contains unrelated fields")

    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _cleanup_expired_computer_data(connection, customer["id"])
        device = connection.execute(
            "SELECT permissions FROM agent_devices WHERE id = ? AND owner_id = ?",
            (device_id, customer["id"]),
        ).fetchone()
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        if permission not in json.loads(device["permissions"]):
            raise HTTPException(status_code=403, detail="Device does not permit this action")
        if action == "click" and "capture_screen" not in json.loads(device["permissions"]):
            raise HTTPException(
                status_code=403,
                detail="Fresh click verification requires screen-capture permission",
            )
        if _agent_safety_stopped(connection, customer["id"]):
            raise HTTPException(status_code=423, detail="Emergency stop is active")

        observation_data = None
        observation_row = None
        if observation_source is not None:
            if not request.observation_id or not request.target_element_id:
                raise HTTPException(
                    status_code=422,
                    detail="A fresh observation and target element are required",
                )
            observation_row = connection.execute(
                """
                SELECT id, source, screen_width, screen_height, elements, expires_at,
                       browser_session_id, page_identity, navigation_generation,
                       image_sha256, active_window_identity, captured_at
                FROM computer_observations
                WHERE id = ? AND owner_id = ? AND device_id = ?
                """,
                (request.observation_id, customer["id"], device_id),
            ).fetchone()
            if observation_row is None or observation_row["expires_at"] <= _utc_text():
                raise HTTPException(status_code=409, detail="Observation is missing or expired")
            observation_data = {
                "observation_id": observation_row["id"],
                "device_id": device_id,
                "source": observation_row["source"],
                "browser_session_id": observation_row["browser_session_id"],
                "page_identity": observation_row["page_identity"],
                "navigation_generation": observation_row["navigation_generation"],
                "image_sha256": observation_row["image_sha256"],
                "active_window_identity": observation_row["active_window_identity"],
                "timestamp": observation_row["captured_at"],
                "expires_at": observation_row["expires_at"],
                "screen_dimensions": {
                    "width": observation_row["screen_width"],
                    "height": observation_row["screen_height"],
                },
                "elements": json.loads(observation_row["elements"]),
            }

        session_id = request.browser_session_id
        normalized_url = request.url
        if request.operation == "browser.navigate":
            if not request.url:
                raise HTTPException(status_code=422, detail="An approved destination URL is required")
            try:
                normalized_url, _addresses = validate_browser_url(
                    request.url, configured_browser_domains()
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if session_id is None:
                session_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO browser_sessions
                    (id, owner_id, device_id, initial_url, status, created_at, expires_at)
                    VALUES (?, ?, ?, ?, 'planned', ?, ?)
                    """,
                    (
                        session_id,
                        customer["id"],
                        device_id,
                        None,
                        _utc_text(),
                        _utc_text(_utc_now() + timedelta(seconds=OBSERVATION_TTL_SECONDS)),
                    ),
                )
            else:
                session = connection.execute(
                    """
                    SELECT id, status, expires_at, page_identity, navigation_generation
                    FROM browser_sessions
                    WHERE id = ? AND owner_id = ? AND device_id = ?
                    """,
                    (session_id, customer["id"], device_id),
                ).fetchone()
                if (
                    session is None
                    or session["expires_at"] <= _utc_text()
                    or session["status"] not in {"planned", "active"}
                ):
                    raise HTTPException(status_code=409, detail="Browser session is unavailable")
        elif action.startswith("browser_"):
            if not session_id:
                raise HTTPException(status_code=422, detail="Browser session ID is required")
            session = connection.execute(
                """
                SELECT id, status, expires_at, page_identity, navigation_generation
                FROM browser_sessions
                WHERE id = ? AND owner_id = ? AND device_id = ?
                """,
                (session_id, customer["id"], device_id),
            ).fetchone()
            if (
                session is None
                or session["expires_at"] <= _utc_text()
                or session["status"] not in {"planned", "active"}
            ):
                raise HTTPException(status_code=409, detail="Browser session is unavailable")
            if request.operation in {
                "browser.fill", "browser.submit", "browser.download"
            } and (
                observation_row is None
                or observation_row["browser_session_id"] != session_id
                or observation_row["page_identity"] != session["page_identity"]
                or observation_row["navigation_generation"]
                != session["navigation_generation"]
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Browser observation is stale or belongs to another page",
                )

        if request.operation == "desktop.click":
            expectation = request.expected_result
            if (
                not isinstance(expectation, dict)
                or set(expectation) != {"kind", "text"}
                or expectation.get("kind") != "element_visible"
                or not isinstance(expectation.get("text"), str)
                or not 1 <= len(expectation["text"]) <= 200
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Desktop click requires an expected visible result",
                )

        if request.operation == "browser.submit":
            expectation = request.expected_result
            if not isinstance(expectation, dict):
                raise HTTPException(status_code=422, detail="Expected result is required")
            if expectation.get("kind") == "url":
                if set(expectation) != {"kind", "url"} or not isinstance(expectation.get("url"), str):
                    raise HTTPException(status_code=422, detail="Expected result is invalid")
                try:
                    validate_browser_url(
                        expectation["url"], configured_browser_domains()
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            elif (
                expectation.get("kind") != "visible_text"
                or set(expectation) != {"kind", "text"}
                or not isinstance(expectation.get("text"), str)
                or not 1 <= len(expectation["text"]) <= 500
            ):
                raise HTTPException(status_code=422, detail="Expected result is invalid")

        try:
            action, permission, confidence, payload = build_action_proposal(
                request.operation,
                instruction=request.instruction,
                observation=observation_data,
                target_element_id=request.target_element_id,
                session_id=session_id,
                url=normalized_url,
                text=request.text,
                expected_result=request.expected_result,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        proposal_id = str(uuid.uuid4())
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        now = _utc_text()
        expires_at = _utc_text(_utc_now() + timedelta(seconds=ACTION_PROPOSAL_TTL_SECONDS))
        digest = payload_hash(
            action,
            payload_json,
            device_id,
            proposal_id=proposal_id,
            instruction=request.instruction,
        )
        connection.execute(
            """
            INSERT INTO computer_action_proposals
            (id, owner_id, device_id, observation_id, browser_session_id, action,
             instruction, target_element_id, permission, confidence, payload, payload_hash,
             expected_result, status, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?)
            """,
            (
                proposal_id,
                customer["id"],
                device_id,
                observation_row["id"] if observation_row is not None else None,
                session_id if action.startswith("browser_") else None,
                action,
                request.instruction.strip(),
                request.target_element_id,
                permission,
                confidence,
                payload_json,
                digest,
                json.dumps(request.expected_result or {}, separators=(",", ":"), sort_keys=True),
                expires_at,
                now,
                now,
            ),
        )
        _agent_audit(
            connection,
            customer["id"],
            "computer_action_proposed",
            device_id=device_id,
            details={"action": action, "proposal_id": proposal_id},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "action_id": proposal_id,
        "action": action,
        "instruction": request.instruction.strip(),
        "payload": payload,
        "required_permission": permission,
        "confidence": confidence,
        "expected_result": request.expected_result,
        "expires_at": expires_at,
        "status": "proposed",
        "browser_session_id": session_id if action.startswith("browser_") else None,
    }


@app.post("/computer/agent/devices/{device_id}/proposal-drafts", status_code=201)
def draft_computer_action_proposal(
    device_id: str,
    request: ComputerActionDraftRequest,
    customer: dict = Depends(get_current_customer),
):
    if os.getenv("NEX_COMPUTER_AGENT_PLANNING_ENABLED", "").strip().lower() != "true":
        raise HTTPException(status_code=409, detail="Computer Agent planning is disabled")
    connection = get_db()
    try:
        device = connection.execute(
            """
            SELECT permissions FROM agent_devices
            WHERE id = ? AND owner_id = ? AND status = 'connected'
            """,
            (device_id, customer["id"]),
        ).fetchone()
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        if _agent_safety_stopped(connection, customer["id"]):
            raise HTTPException(status_code=423, detail="Emergency stop is active")
        observation = connection.execute(
            """
            SELECT id, source, screen_width, screen_height, elements
            FROM computer_observations
            WHERE id = ? AND owner_id = ? AND device_id = ? AND expires_at > ?
            """,
            (
                request.observation_id,
                customer["id"],
                device_id,
                _utc_text(),
            ),
        ).fetchone()
    finally:
        connection.close()
    if observation is None:
        raise HTTPException(status_code=404, detail="Observation not found")

    permitted = set(json.loads(device["permissions"]))
    allowed_operations = SOURCE_OPERATIONS.get(observation["source"], set()) & {
        operation
        for operation, (_action, permission, _source, _element_type) in OPERATION_ACTIONS.items()
        if permission in permitted
    }
    if not DESKTOP_EXECUTION_ENABLED:
        allowed_operations.discard("desktop.click")
    if not BROWSER_AUTOMATION_ENABLED or not request.browser_session_id:
        allowed_operations.difference_update(
            {"browser.fill", "browser.submit", "browser.download"}
        )
    if request.text is None:
        allowed_operations.discard("browser.fill")
    if request.expected_result is None:
        allowed_operations.difference_update({"desktop.click", "browser.submit"})
    if not allowed_operations:
        raise HTTPException(
            status_code=403,
            detail="No permitted computer action matches this observation",
        )

    elements = json.loads(observation["elements"])
    context = {
        "source": observation["source"],
        "screen_dimensions": {
            "width": observation["screen_width"],
            "height": observation["screen_height"],
        },
        "allowed_operations": sorted(allowed_operations),
        "elements": [
            {
                "element_id": element["element_id"],
                "type": element["type"],
                "description": element["description"],
                "confidence": element["confidence"],
                "ambiguous": element["ambiguous"],
            }
            for element in elements
        ],
    }
    result = execute_agent(
        "computer_agent",
        "Select one observed computer action proposal",
        request.instruction,
        json.dumps(context, separators=(",", ":"), sort_keys=True),
    )
    if result.get("status") != "completed":
        raise HTTPException(status_code=503, detail="Computer Agent planning failed")
    raw_candidate = result.get("result")
    if not isinstance(raw_candidate, str):
        raise HTTPException(status_code=503, detail="Computer Agent planning failed")
    try:
        candidate = parse_agent_candidate(
            extract_handoff_context(raw_candidate),
            allowed_operations,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not candidate["target_element_id"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Computer Agent could not select an unambiguous target",
                "rationale": candidate["rationale"],
            },
        )

    proposal_request = ComputerActionProposalRequest(
        instruction=request.instruction,
        operation=candidate["operation"],
        observation_id=request.observation_id,
        browser_session_id=request.browser_session_id,
        target_element_id=candidate["target_element_id"],
        text=request.text,
        expected_result=request.expected_result,
    )
    proposal = create_computer_action_proposal(
        device_id,
        proposal_request,
        customer,
    )
    return {**proposal, "planner_rationale": candidate["rationale"]}


@app.get("/computer/agent/observations/{observation_id}")
def get_computer_observation(
    observation_id: str,
    customer: dict = Depends(get_current_customer),
):
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _cleanup_expired_computer_data(connection, customer["id"])
        row = connection.execute(
            """
            SELECT id AS observation_id, device_id, source, screen_width,
                   screen_height, provider, elements, browser_session_id,
                   page_identity, navigation_generation, captured_at, expires_at
            FROM computer_observations
            WHERE id = ? AND owner_id = ? AND expires_at > ?
            """,
            (observation_id, customer["id"], _utc_text()),
        ).fetchone()
        connection.commit()
    finally:
        connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Observation not found")
    return {
        **dict(row),
        "screen_dimensions": {"width": row["screen_width"], "height": row["screen_height"]},
        "elements": json.loads(row["elements"]),
    }


@app.get("/computer/agent/proposals/{proposal_id}")
def get_computer_action_proposal(
    proposal_id: str,
    customer: dict = Depends(get_current_customer),
):
    connection = get_db()
    try:
        row = connection.execute(
            """
            SELECT id AS action_id, device_id, observation_id, browser_session_id,
                   action, instruction, target_element_id, permission AS required_permission,
                   confidence, payload, expected_result, verification_status,
                   status, expires_at, created_at
            FROM computer_action_proposals
            WHERE id = ? AND owner_id = ?
            """,
            (proposal_id, customer["id"]),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Action proposal not found")
    return {
        **dict(row),
        "payload": json.loads(row["payload"]),
        "expected_result": json.loads(row["expected_result"]),
    }


@app.post("/computer/agent/proposals/{proposal_id}/verify")
def verify_computer_action(
    proposal_id: str,
    request: ComputerActionVerificationRequest,
    customer: dict = Depends(get_current_customer),
):
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        proposal = connection.execute(
            """
            SELECT p.owner_id, p.device_id, p.action, p.expected_result,
                   p.verification_status, c.id AS command_id, c.status AS command_status,
                   c.updated_at AS command_updated_at, c.result,
                   o.source, o.elements, o.captured_at
            FROM computer_action_proposals p
            JOIN agent_commands c ON c.proposal_id = p.id AND c.owner_id = p.owner_id
            JOIN computer_observations o
              ON o.id = ? AND o.owner_id = p.owner_id AND o.device_id = p.device_id
            WHERE p.id = ? AND p.owner_id = ? AND o.expires_at > ?
            """,
            (request.observation_id, proposal_id, customer["id"], _utc_text()),
        ).fetchone()
        if (
            proposal is None
            or proposal["action"] != "click"
            or proposal["command_status"] != "completed"
            or proposal["verification_status"] != "pending"
            or proposal["source"] != "screen"
            or proposal["captured_at"] <= proposal["command_updated_at"]
        ):
            connection.rollback()
            raise HTTPException(
                status_code=409,
                detail="A later authorized screen observation is required",
            )
        expected = json.loads(proposal["expected_result"])
        if (
            not isinstance(expected, dict)
            or expected.get("kind") != "element_visible"
            or not isinstance(expected.get("text"), str)
        ):
            connection.rollback()
            raise HTTPException(status_code=409, detail="Proposal has no verifiable expected result")
        matched = any(
            isinstance(element, dict)
            and isinstance(element.get("description"), str)
            and expected["text"].casefold() in element["description"].casefold()
            and isinstance(element.get("confidence"), (int, float))
            and not isinstance(element.get("confidence"), bool)
            and element["confidence"] >= MIN_ACTION_CONFIDENCE
            and element.get("ambiguous") is False
            for element in json.loads(proposal["elements"])
        )
        if not matched:
            connection.rollback()
            raise HTTPException(
                status_code=409,
                detail="Expected result was not detected; do not retry the action automatically",
            )
        result = json.loads(proposal["result"] or "{}")
        result["verification_status"] = "verified"
        result["message"] = "Approved desktop action verified by a later observation"
        now = _utc_text()
        connection.execute(
            """
            UPDATE computer_action_proposals
            SET verification_status = 'verified', updated_at = ?
            WHERE id = ? AND owner_id = ? AND verification_status = 'pending'
            """,
            (now, proposal_id, customer["id"]),
        )
        connection.execute(
            "UPDATE agent_commands SET result = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
            (
                json.dumps(result),
                now,
                proposal["command_id"],
                customer["id"],
            ),
        )
        _agent_audit(
            connection,
            customer["id"],
            "computer_action_verified",
            device_id=proposal["device_id"],
            command_id=proposal["command_id"],
            details={"proposal_id": proposal_id, "observation_id": request.observation_id},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "action_id": proposal_id,
        "command_id": proposal["command_id"],
        "verification_status": "verified",
    }


@app.post(
    "/computer/agent/devices/{device_id}/commands/{command_id}/observation",
    status_code=201,
)
async def upload_agent_observation(
    device_id: str,
    command_id: str,
    request: Request,
):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
        raise HTTPException(status_code=415, detail="Screenshot must use application/octet-stream")
    credential = request.headers.get("X-Device-Credential", "")
    if not 32 <= len(credential) <= 128:
        raise HTTPException(status_code=404, detail="Authorized capture command not found")
    supplied_hash = hashlib.sha256(credential.encode("utf-8")).hexdigest()

    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        device = connection.execute(
            """
            SELECT owner_id, credential_hash, status, permissions
            FROM agent_devices WHERE id = ?
            """,
            (device_id,),
        ).fetchone()
        command = connection.execute(
            """
            SELECT id FROM agent_commands
            WHERE id = ? AND device_id = ? AND action = 'capture_screen'
              AND status = 'executing'
            """,
            (command_id, device_id),
        ).fetchone()
        if (
            device is None
            or command is None
            or not hmac.compare_digest(supplied_hash, device["credential_hash"])
            or device["status"] != "connected"
            or "capture_screen" not in json.loads(device["permissions"])
            or _agent_safety_stopped(connection, device["owner_id"])
        ):
            connection.rollback()
            raise HTTPException(status_code=404, detail="Authorized capture command not found")
        if not _consume_agent_rate_limit(
            connection, device["owner_id"], "observation", 10, window_seconds=60
        ):
            _agent_audit(
                connection,
                device["owner_id"],
                "rate_limit_exceeded",
                device_id=device_id,
                command_id=command_id,
                details={"scope": "observation"},
            )
            connection.commit()
            raise HTTPException(status_code=429, detail="Observation rate limit exceeded")
        owner_id = device["owner_id"]
        approved_payload = json.loads(command["payload"])
        connection.commit()
    finally:
        connection.close()

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_SCREENSHOT_BYTES:
            raise HTTPException(status_code=413, detail="Screenshot exceeds the byte limit")
        body.extend(chunk)
    if not body:
        raise HTTPException(status_code=400, detail="Screenshot is empty")
    image_sha256 = hashlib.sha256(body).hexdigest()
    active_window_identity = request.headers.get("X-Active-Window-Identity")
    if active_window_identity is not None and not re.fullmatch(
        r"[a-f0-9]{64}", active_window_identity
    ):
        raise HTTPException(status_code=422, detail="Active-window identity is invalid")
    try:
        observation = await asyncio.to_thread(
            VisionEngine().analyze_screenshot,
            bytes(body),
            device_id,
            approved_payload.get("vision_provider", "mock"),
            approved_payload.get("redaction_boxes"),
        )
    except (ValueError, RuntimeError) as exc:
        _audit_agent_protocol_failure(owner_id, device_id, "screenshot validation or analysis failed")
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        command = connection.execute(
            """
            SELECT c.owner_id, c.status, d.status AS device_status
            FROM agent_commands c
            JOIN agent_devices d ON d.id = c.device_id AND d.owner_id = c.owner_id
            WHERE c.id = ? AND c.device_id = ? AND c.action = 'capture_screen'
            """,
            (command_id, device_id),
        ).fetchone()
        if (
            command is None
            or command["owner_id"] != owner_id
            or command["status"] != "executing"
            or command["device_status"] != "connected"
            or _agent_safety_stopped(connection, owner_id)
        ):
            connection.rollback()
            raise HTTPException(status_code=409, detail="Capture authorization expired")
        _cleanup_expired_computer_data(connection, owner_id)
        stored = _store_observation(
            connection,
            owner_id,
            device_id,
            "screen",
            observation["screen_dimensions"]["width"],
            observation["screen_dimensions"]["height"],
            observation["provider"],
            observation["elements"],
            command_id=command_id,
            image_sha256=image_sha256,
            observation_id=observation["observation_id"],
            active_window_identity=active_window_identity,
        )
        _agent_audit(
            connection,
            owner_id,
            "screen_observation_created",
            device_id=device_id,
            command_id=command_id,
            details={"observation_id": stored["observation_id"], "element_count": len(stored["elements"])},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "status": "created",
        "observation_id": stored["observation_id"],
        "device_id": device_id,
        "screen_dimensions": stored["screen_dimensions"],
        "element_count": len(stored["elements"]),
        "captured_at": stored["timestamp"],
        "expires_at": stored["expires_at"],
    }


@app.post("/computer/agent/devices/{device_id}/commands", status_code=202)
def request_agent_command(
    device_id: str,
    request: AgentCommandRequest,
    customer: dict = Depends(get_current_customer),
):
    _validate_agent_action(request.action, request.payload)
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
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

        proposal = None
        if request.action.startswith("browser_") or request.action == "click":
            if not request.proposal_id:
                raise HTTPException(status_code=422, detail="Action proposal approval is required")
            proposal = connection.execute(
                """
                SELECT id, action, instruction, payload, payload_hash, status, expires_at
                FROM computer_action_proposals
                WHERE id = ? AND owner_id = ? AND device_id = ?
                """,
                (request.proposal_id, customer["id"], device_id),
            ).fetchone()
            if (
                proposal is None
                or proposal["status"] != "proposed"
                or proposal["expires_at"] <= _utc_text()
                or proposal["action"] != request.action
                or proposal["payload"] != json.dumps(
                    request.payload, separators=(",", ":"), sort_keys=True
                )
                or not hmac.compare_digest(
                    payload_hash(
                        request.action,
                        request.payload,
                        device_id,
                        proposal_id=request.proposal_id,
                        instruction=proposal["instruction"],
                    ),
                    proposal["payload_hash"],
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Action proposal is expired, changed, or already used",
                )
        elif request.proposal_id:
            raise HTTPException(status_code=422, detail="Proposal does not match the action")

        now = _utc_now()
        command_id = str(uuid.uuid4())
        approval_id = str(uuid.uuid4())
        approval_token = secrets.token_urlsafe(32)
        payload_json = json.dumps(
            request.payload,
            separators=(",", ":"),
            sort_keys=True,
        )
        expires_at = _utc_text(now + timedelta(seconds=AGENT_APPROVAL_TTL_SECONDS))
        try:
            connection.execute(
                """
                INSERT INTO agent_commands
                (id, owner_id, device_id, client_request_id, action, payload,
                 proposal_id, status, created_at, updated_at, deadline_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_approval', ?, ?, ?)
                """,
                (
                    command_id,
                    customer["id"],
                    device_id,
                    request.client_request_id,
                    request.action,
                    payload_json,
                    request.proposal_id,
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
            (id, owner_id, command_id, token_hash, payload_hash, status, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                approval_id,
                customer["id"],
                command_id,
                hashlib.sha256(approval_token.encode("utf-8")).hexdigest(),
                payload_hash(
                    request.action,
                    payload_json,
                    device_id,
                    proposal_id=request.proposal_id,
                    instruction=proposal["instruction"] if proposal is not None else None,
                ),
                expires_at,
                _utc_text(now),
            ),
        )
        if proposal is not None:
            connection.execute(
                """
                UPDATE computer_action_proposals
                SET status = 'pending_approval', updated_at = ?
                WHERE id = ? AND owner_id = ? AND status = 'proposed'
                """,
                (_utc_text(now), request.proposal_id, customer["id"]),
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
        "status": "pending_approval",
        "approval": {
            "request_id": approval_id,
            "expires_at": expires_at,
            "approval_token": approval_token,
        },
        "execution_enabled": _action_execution_enabled(request.action),
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
              AND c.status = 'pending_approval'
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
            SELECT a.id, a.command_id, a.token_hash, a.expires_at, c.proposal_id
            FROM agent_approvals a
            JOIN agent_commands c ON c.id = a.command_id AND c.owner_id = a.owner_id
            WHERE a.id = ? AND a.owner_id = ? AND a.status = 'pending'
            """,
            (request_id, customer["id"]),
        ).fetchone()
        if approval is None:
            _agent_audit(
                connection,
                customer["id"],
                "approval_attempt_rejected",
                details={"reason": "not found or already consumed"},
            )
            connection.commit()
            raise HTTPException(status_code=404, detail="Approval request not found")
        if not hmac.compare_digest(supplied_hash, approval["token_hash"]):
            _agent_audit(
                connection,
                customer["id"],
                "approval_attempt_rejected",
                command_id=approval["command_id"],
                details={"reason": "invalid token"},
            )
            connection.commit()
            raise HTTPException(status_code=403, detail="Invalid approval token")
        now = _utc_text()
        if approval["expires_at"] <= now:
            connection.execute(
                """
                UPDATE agent_approvals SET status = 'expired', consumed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, request_id),
            )
            connection.execute(
                """
                UPDATE agent_commands SET status = 'expired', updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (now, approval["command_id"], customer["id"]),
            )
            if approval["proposal_id"]:
                connection.execute(
                    """
                    UPDATE computer_action_proposals SET status = 'expired', updated_at = ?
                    WHERE id = ? AND owner_id = ?
                    """,
                    (now, approval["proposal_id"], customer["id"]),
                )
            _agent_audit(
                connection,
                customer["id"],
                "approval_expired",
                command_id=approval["command_id"],
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
            WHERE id = ? AND owner_id = ? AND status = 'pending_approval'
            """,
            (now, approval["command_id"], customer["id"]),
        )
        if approval["proposal_id"]:
            connection.execute(
                """
                UPDATE computer_action_proposals SET status = 'failed', updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (now, approval["proposal_id"], customer["id"]),
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
            SELECT a.id, a.command_id, a.token_hash, a.payload_hash, a.expires_at,
                   c.device_id, c.action, c.payload, c.proposal_id,
                   p.status AS proposal_status, p.expires_at AS proposal_expires_at,
                   p.action AS proposal_action, p.payload AS proposal_payload,
                   p.payload_hash AS proposal_payload_hash,
                   p.instruction AS proposal_instruction
            FROM agent_approvals a
            JOIN agent_commands c ON c.id = a.command_id
            LEFT JOIN computer_action_proposals p
              ON p.id = c.proposal_id AND p.owner_id = c.owner_id
            WHERE a.id = ? AND a.owner_id = ? AND a.status = 'pending'
              AND c.owner_id = ?
              AND c.status = 'pending_approval'
            """,
            (request_id, customer["id"], customer["id"]),
        ).fetchone()
        if approval is None:
            _agent_audit(
                connection,
                customer["id"],
                "approval_attempt_rejected",
                details={"reason": "not found or already consumed"},
            )
            connection.commit()
            raise HTTPException(status_code=404, detail="Approval request not found")
        if not hmac.compare_digest(supplied_hash, approval["token_hash"]):
            _agent_audit(
                connection,
                customer["id"],
                "approval_attempt_rejected",
                command_id=approval["command_id"],
                details={"reason": "invalid token"},
            )
            connection.commit()
            raise HTTPException(status_code=403, detail="Invalid approval token")
        now = _utc_text()
        if approval["expires_at"] <= now:
            connection.execute(
                """
                UPDATE agent_approvals SET status = 'expired', consumed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, request_id),
            )
            connection.execute(
                """
                UPDATE agent_commands SET status = 'expired', updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (now, approval["command_id"], customer["id"]),
            )
            if approval["proposal_id"]:
                connection.execute(
                    """
                    UPDATE computer_action_proposals SET status = 'expired', updated_at = ?
                    WHERE id = ? AND owner_id = ?
                    """,
                    (now, approval["proposal_id"], customer["id"]),
                )
            _agent_audit(
                connection,
                customer["id"],
                "approval_expired",
                command_id=approval["command_id"],
            )
            connection.commit()
            raise HTTPException(status_code=409, detail="Approval request has expired")

        try:
            current_payload_hash = payload_hash(
                approval["action"],
                approval["payload"],
                approval["device_id"],
                proposal_id=approval["proposal_id"],
                instruction=approval["proposal_instruction"],
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            current_payload_hash = ""
        if not hmac.compare_digest(current_payload_hash, approval["payload_hash"]):
            connection.execute(
                """
                UPDATE agent_approvals SET status = 'denied', consumed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, request_id),
            )
            connection.execute(
                """
                UPDATE agent_commands
                SET status = 'failed', updated_at = ?
                WHERE id = ? AND owner_id = ? AND status = 'pending_approval'
                """,
                (now, approval["command_id"], customer["id"]),
            )
            if approval["proposal_id"]:
                connection.execute(
                    """
                    UPDATE computer_action_proposals SET status = 'failed', updated_at = ?
                    WHERE id = ? AND owner_id = ?
                    """,
                    (now, approval["proposal_id"], customer["id"]),
                )
            _agent_audit(
                connection,
                customer["id"],
                "approval_payload_mismatch",
                device_id=approval["device_id"],
                command_id=approval["command_id"],
            )
            connection.commit()
            raise HTTPException(status_code=409, detail="Command payload changed after approval request")

        if approval["proposal_id"] and (
            approval["proposal_status"] != "pending_approval"
            or approval["proposal_expires_at"] <= now
            or approval["proposal_action"] != approval["action"]
            or approval["proposal_payload"] != approval["payload"]
            or not hmac.compare_digest(
                payload_hash(
                    approval["action"],
                    approval["payload"],
                    approval["device_id"],
                    proposal_id=approval["proposal_id"],
                    instruction=approval["proposal_instruction"],
                ),
                approval["proposal_payload_hash"],
            )
        ):
            connection.execute(
                """
                UPDATE agent_approvals SET status = 'expired', consumed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, request_id),
            )
            connection.execute(
                """
                UPDATE agent_commands SET status = 'expired', updated_at = ?
                WHERE id = ? AND owner_id = ? AND status = 'pending_approval'
                """,
                (now, approval["command_id"], customer["id"]),
            )
            connection.execute(
                """
                UPDATE computer_action_proposals SET status = 'expired', updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (now, approval["proposal_id"], customer["id"]),
            )
            connection.commit()
            raise HTTPException(status_code=409, detail="Action proposal changed or expired")

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

        approval_updated = connection.execute(
            """
            UPDATE agent_approvals SET status = 'approved', consumed_at = ?
            WHERE id = ? AND owner_id = ? AND status = 'pending'
            """,
            (now, request_id, customer["id"]),
        ).rowcount
        command_updated = connection.execute(
            """
            UPDATE agent_commands SET status = 'approved', updated_at = ?
            WHERE id = ? AND owner_id = ? AND status = 'pending_approval'
            """,
            (now, approval["command_id"], customer["id"]),
        ).rowcount
        if approval_updated != 1 or command_updated != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Approval was already consumed")
        if approval["proposal_id"]:
            connection.execute(
                """
                UPDATE computer_action_proposals SET status = 'approved', updated_at = ?
                WHERE id = ? AND owner_id = ? AND status = 'pending_approval'
                """,
                (now, approval["proposal_id"], customer["id"]),
            )
        _agent_audit(
            connection,
            customer["id"],
            "command_approved",
            device_id=approval["device_id"],
            command_id=approval["command_id"],
            details={"execution_enabled": _action_execution_enabled(approval["action"])},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    loop = asyncio.get_running_loop()
    response_future = loop.create_future()
    dispatch_failed = False
    try:
        async with AGENT_DISPATCH_LOCK:
            dispatch = _begin_agent_dispatch(approval["command_id"], customer["id"])
            if dispatch is None:
                raise HTTPException(
                    status_code=409,
                    detail="Command was cancelled or the device became unavailable before dispatch",
                )
            command = {
                "type": "command",
                "command_id": approval["command_id"],
                "action": dispatch["action"],
                "payload": json.loads(dispatch["payload"]),
                "execution_enabled": _action_execution_enabled(dispatch["action"]),
            }
            try:
                validate_command(command)
            except ValueError:
                _fail_agent_command(
                    approval["command_id"],
                    customer["id"],
                    "Command validation failed",
                )
                raise HTTPException(
                    status_code=422,
                    detail="Command failed Local Agent validation",
                )

            websocket = AGENT_CONNECTIONS.get(dispatch["device_id"])
            if websocket is None:
                _fail_agent_command(
                    approval["command_id"],
                    customer["id"],
                    "Device disconnected before dispatch",
                )
                raise HTTPException(
                    status_code=409,
                    detail="Device disconnected before dispatch",
                )

            PENDING_AGENT_COMMANDS[approval["command_id"]] = response_future
            try:
                await websocket.send_json(command)
            except Exception:
                _fail_agent_command(
                    approval["command_id"],
                    customer["id"],
                    "Local Agent dispatch failed",
                )
                dispatch_failed = True

        if dispatch_failed:
            result = {
                "command_id": approval["command_id"],
                "action": approval["action"],
                "success": False,
                "message": "Local Agent dispatch failed",
            }
        else:
            result = await asyncio.wait_for(
                response_future,
                timeout=(
                    AGENT_SCREEN_ANALYSIS_TIMEOUT_SECONDS
                    if approval["action"] == "capture_screen"
                    else AGENT_BROWSER_TIMEOUT_SECONDS
                    if approval["action"].startswith("browser_")
                    else AGENT_COMMAND_TIMEOUT_SECONDS
                ),
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
    except HTTPException:
        raise
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
        "execution_enabled": _action_execution_enabled(approval["action"]),
    }


def _begin_agent_dispatch(command_id, owner_id):
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        command = connection.execute(
            """
            SELECT c.id, c.device_id, c.action, c.payload, c.proposal_id, c.status,
                   a.id AS approval_ref, a.payload_hash,
                   d.status AS device_status, d.permissions
            FROM agent_commands c
            JOIN agent_approvals a ON a.command_id = c.id AND a.owner_id = c.owner_id
            JOIN agent_devices d ON d.id = c.device_id AND d.owner_id = c.owner_id
            WHERE c.id = ? AND c.owner_id = ? AND c.status = 'approved'
              AND a.status = 'approved'
            """,
            (command_id, owner_id),
        ).fetchone()
        if command is None:
            connection.rollback()
            return None
        approval_ref = command["approval_ref"]

        proposal_instruction = None
        if command["proposal_id"]:
            proposal = connection.execute(
                """
                SELECT action, instruction, payload, payload_hash, status, expires_at
                FROM computer_action_proposals
                WHERE id = ? AND owner_id = ? AND device_id = ?
                """,
                (command["proposal_id"], owner_id, command["device_id"]),
            ).fetchone()
            if (
                proposal is None
                or proposal["status"] != "approved"
                or proposal["expires_at"] <= _utc_text()
                or proposal["action"] != command["action"]
                or proposal["payload"] != command["payload"]
                or not hmac.compare_digest(
                    payload_hash(
                        command["action"],
                        command["payload"],
                        command["device_id"],
                        proposal_id=command["proposal_id"],
                        instruction=proposal["instruction"],
                    ),
                    proposal["payload_hash"],
                )
            ):
                connection.rollback()
                return None
            proposal_instruction = proposal["instruction"]
            if command["action"] in {
                "browser_fill", "browser_submit", "browser_download"
            }:
                try:
                    browser_payload = json.loads(command["payload"])
                except (TypeError, json.JSONDecodeError):
                    connection.rollback()
                    return None
                browser_session = connection.execute(
                    """
                    SELECT status, expires_at, page_identity, navigation_generation
                    FROM browser_sessions
                    WHERE id = ? AND owner_id = ? AND device_id = ?
                    """,
                    (
                        browser_payload.get("session_id"),
                        owner_id,
                        command["device_id"],
                    ),
                ).fetchone()
                if (
                    browser_session is None
                    or browser_session["status"] != "active"
                    or browser_session["expires_at"] <= _utc_text()
                    or browser_session["page_identity"] != browser_payload.get("page_identity")
                    or browser_session["navigation_generation"]
                    != browser_payload.get("navigation_generation")
                ):
                    connection.rollback()
                    return None

        if _agent_safety_stopped(connection, owner_id):
            now = _utc_text()
            result = {
                "command_id": command_id,
                "action": command["action"],
                "success": False,
                "message": "Emergency stop activated before dispatch",
            }
            connection.execute(
                """
                UPDATE agent_commands
                SET status = 'cancelled', result = ?, updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (json.dumps(result), now, command_id),
            )
            _agent_audit(connection, owner_id, "command_cancelled_emergency_stop", device_id=command["device_id"], command_id=command_id)
            connection.commit()
            return None

        if command["device_status"] != "connected" or command["device_id"] not in AGENT_CONNECTIONS:
            now = _utc_text()
            result = {
                "command_id": command_id,
                "action": command["action"],
                "success": False,
                "message": "Device unavailable before dispatch",
            }
            connection.execute(
                """
                UPDATE agent_commands
                SET status = 'failed', result = ?, updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (json.dumps(result), now, command_id),
            )
            _agent_audit(connection, owner_id, "command_failed", device_id=command["device_id"], command_id=command_id, details={"reason": "device unavailable before dispatch"})
            connection.commit()
            return None

        try:
            permissions = json.loads(command["permissions"])
        except (TypeError, json.JSONDecodeError):
            permissions = []
        if (
            not isinstance(permissions, list)
            or command["action"] not in permissions
            or (
                command["action"] == "click"
                and "capture_screen" not in permissions
            )
        ):
            now = _utc_text()
            result = {
                "command_id": command_id,
                "action": command["action"],
                "success": False,
                "message": "Device permission no longer allows this action",
            }
            connection.execute(
                """
                UPDATE agent_commands
                SET status = 'failed', result = ?, updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (json.dumps(result), now, command_id),
            )
            _agent_audit(connection, owner_id, "command_failed", device_id=command["device_id"], command_id=command_id, details={"reason": "device permission revoked"})
            connection.commit()
            return None

        if _action_execution_enabled(command["action"]):
            capability = connection.execute(
                """
                SELECT capabilities FROM agent_desktop_capabilities
                WHERE device_id = ? AND owner_id = ?
                """,
                (command["device_id"], owner_id),
            ).fetchone()
            capability_allowed = False
            if capability is not None:
                try:
                    reported = json.loads(capability["capabilities"])
                    capability_allowed = (
                        (
                            reported.get("browser_automation") is True
                            if command["action"].startswith("browser_")
                            else reported.get("desktop_execution") is True
                        )
                        and reported.get(command["action"]) is True
                        and (
                            command["action"] != "click"
                            or reported.get("capture_screen") is True
                        )
                    )
                except (TypeError, json.JSONDecodeError):
                    capability_allowed = False
            if not capability_allowed:
                now = _utc_text()
                result = {
                    "command_id": command_id,
                    "action": command["action"],
                    "success": False,
                    "message": "Local Agent did not report this desktop capability",
                }
                connection.execute(
                    """
                    UPDATE agent_commands
                    SET status = 'failed', result = ?, updated_at = ?
                    WHERE id = ? AND status = 'approved'
                    """,
                    (json.dumps(result), now, command_id),
                )
                _agent_audit(
                    connection,
                    owner_id,
                    "command_failed",
                    device_id=command["device_id"],
                    command_id=command_id,
                    details={"reason": "required desktop capability unavailable"},
                )
                connection.commit()
                return None

        try:
            current_hash = payload_hash(
                command["action"],
                command["payload"],
                command["device_id"],
                proposal_id=command["proposal_id"],
                instruction=proposal_instruction,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            current_hash = ""
        if not hmac.compare_digest(current_hash, command["payload_hash"]):
            now = _utc_text()
            result = {
                "command_id": command_id,
                "action": command["action"],
                "success": False,
                "message": "Approved payload integrity check failed",
            }
            connection.execute(
                """
                UPDATE agent_commands
                SET status = 'failed', result = ?, updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (json.dumps(result), now, command_id),
            )
            _agent_audit(connection, owner_id, "command_failed", device_id=command["device_id"], command_id=command_id, details={"reason": "approved payload integrity check failed"})
            connection.commit()
            return None

        if command["action"] == "click":
            try:
                click_payload = json.loads(command["payload"])
                observation = connection.execute(
                    """
                    SELECT source, screen_width, screen_height, image_sha256,
                           active_window_identity, elements, captured_at, expires_at
                    FROM computer_observations
                    WHERE id = ? AND owner_id = ? AND device_id = ?
                    """,
                    (
                        click_payload.get("observation_id"),
                        owner_id,
                        command["device_id"],
                    ),
                ).fetchone()
                elements = json.loads(observation["elements"]) if observation else []
                target = next(
                    (
                        item for item in elements
                        if isinstance(item, dict)
                        and item.get("element_id") == click_payload.get("target_element_id")
                    ),
                    None,
                )
                target_box = target.get("bounding_box") if target else None
                observation_valid = (
                    observation is not None
                    and observation["source"] == "screen"
                    and observation["expires_at"] > _utc_text()
                    and observation["image_sha256"] == click_payload.get("observation_image_sha256")
                    and observation["active_window_identity"]
                    == click_payload.get("active_window_identity")
                    and observation["captured_at"] == click_payload.get("observation_captured_at")
                    and observation["screen_width"] == click_payload.get("screen_width")
                    and observation["screen_height"] == click_payload.get("screen_height")
                    and command["device_id"] == click_payload.get("device_id")
                    and target is not None
                    and target.get("type") == "button"
                    and target_box == click_payload.get("target_bbox")
                    and isinstance(target_box, dict)
                    and click_payload.get("x")
                    == int(target_box["x"] + target_box["width"] / 2)
                    and click_payload.get("y")
                    == int(target_box["y"] + target_box["height"] / 2)
                    and command["approval_ref"]
                )
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                observation_valid = False
            if not observation_valid:
                now = _utc_text()
                result = {
                    "command_id": command_id,
                    "action": "click",
                    "success": False,
                    "message": "Approved desktop observation is stale or invalid; obtain a new observation and approval",
                    "failure_code": "desktop_click_stale",
                }
                connection.execute(
                    """
                    UPDATE agent_commands
                    SET status = 'failed', result = ?, updated_at = ?
                    WHERE id = ? AND owner_id = ? AND status = 'approved'
                    """,
                    (json.dumps(result), now, command_id, owner_id),
                )
                if command["proposal_id"]:
                    connection.execute(
                        """
                        UPDATE computer_action_proposals SET status = 'failed',
                            verification_status = 'failed', updated_at = ?
                        WHERE id = ? AND owner_id = ?
                        """,
                        (now, command["proposal_id"], owner_id),
                    )
                _agent_audit(
                    connection,
                    owner_id,
                    "desktop_click_rejected_stale",
                    device_id=command["device_id"],
                    command_id=command_id,
                    details={
                        "reason": "approved observation binding invalid",
                        "observation_id": click_payload.get("observation_id"),
                    },
                )
                connection.commit()
                return None

        connection.execute(
            "UPDATE agent_commands SET status = 'queued', updated_at = ? WHERE id = ? AND status = 'approved'",
            (_utc_text(), command_id),
        )
        if command["proposal_id"]:
            connection.execute(
                """
                UPDATE computer_action_proposals SET status = 'executing', updated_at = ?
                WHERE id = ? AND owner_id = ? AND status = 'approved'
                """,
                (_utc_text(), command["proposal_id"], owner_id),
            )
        _agent_audit(connection, owner_id, "command_queued", device_id=command["device_id"], command_id=command_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        command = connection.execute(
            """
            SELECT c.device_id, c.action, c.payload, d.status AS device_status
            FROM agent_commands c
            JOIN agent_devices d ON d.id = c.device_id AND d.owner_id = c.owner_id
            WHERE c.id = ? AND c.owner_id = ? AND c.status = 'queued'
            """,
            (command_id, owner_id),
        ).fetchone()
        if command is None:
            connection.rollback()
            return None
        stopped = _agent_safety_stopped(connection, owner_id)
        if (
            command["device_status"] != "connected"
            or command["device_id"] not in AGENT_CONNECTIONS
            or stopped
        ):
            now = _utc_text()
            status = "cancelled" if stopped else "failed"
            message = (
                "Emergency stop activated before dispatch"
                if stopped
                else "Device became unavailable before dispatch"
            )
            result = {
                "command_id": command_id,
                "action": command["action"],
                "success": False,
                "message": message,
            }
            connection.execute(
                """
                UPDATE agent_commands
                SET status = ?, result = ?, updated_at = ?
                WHERE id = ? AND owner_id = ? AND status = 'queued'
                """,
                (status, json.dumps(result), now, command_id, owner_id),
            )
            _agent_audit(
                connection,
                owner_id,
                "command_cancelled_emergency_stop" if stopped else "command_failed",
                device_id=command["device_id"],
                command_id=command_id,
                details={"reason": message},
            )
            connection.commit()
            return None
        now = _utc_text()
        timeout_seconds = (
            AGENT_SCREEN_ANALYSIS_TIMEOUT_SECONDS
            if command["action"] == "capture_screen"
            else AGENT_BROWSER_TIMEOUT_SECONDS
            if command["action"].startswith("browser_")
            else AGENT_COMMAND_TIMEOUT_SECONDS
        )
        deadline = _utc_text(_utc_now() + timedelta(seconds=timeout_seconds))
        claimed = connection.execute(
            """
            UPDATE agent_commands
            SET status = 'executing', updated_at = ?, deadline_at = ?
            WHERE id = ? AND owner_id = ? AND status = 'queued'
            """,
            (now, deadline, command_id, owner_id),
        ).rowcount
        if claimed != 1:
            connection.rollback()
            return None
        connection.execute(
            """
            INSERT INTO agent_execution_sessions
            (id, owner_id, device_id, command_id, adapter, state, started_at)
            VALUES (?, ?, ?, ?, 'desktop_runtime', 'started', ?)
            ON CONFLICT(command_id) DO UPDATE SET
                state = 'started',
                started_at = excluded.started_at,
                ended_at = NULL
            """,
            (
                str(uuid.uuid4()),
                owner_id,
                command["device_id"],
                command_id,
                now,
            ),
        )
        _agent_audit(
            connection,
            owner_id,
            "command_dispatched",
            device_id=command["device_id"],
            command_id=command_id,
            details={"execution_enabled": _action_execution_enabled(command["action"])},
        )
        connection.commit()
        dispatch_command = dict(command)
        if dispatch_command["action"] == "click":
            command_payload = json.loads(dispatch_command["payload"])
            command_payload["approval_ref"] = approval_ref
            dispatch_command["payload"] = json.dumps(
                command_payload, separators=(",", ":"), sort_keys=True
            )
        return dispatch_command
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _fail_agent_command(command_id, owner_id, message):
    connection = get_db()
    try:
        with connection:
            row = connection.execute(
                """
                SELECT device_id, action FROM agent_commands
                WHERE id = ? AND owner_id = ? AND status = 'executing'
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
                WHERE id = ? AND owner_id = ? AND status = 'executing'
                """,
                (json.dumps(result), _utc_text(), command_id, owner_id),
            )
            connection.execute(
                """
                UPDATE agent_execution_sessions
                SET state = 'failed', ended_at = ?
                WHERE owner_id = ? AND command_id = ?
                """,
                (_utc_text(), owner_id, command_id),
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
            SELECT id, owner_id, action, payload, proposal_id, deadline_at
            FROM agent_commands
            WHERE id = ? AND device_id = ? AND status = 'executing'
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
                    command_id=command["id"] if command is not None else None,
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

        command_payload = json.loads(command["payload"])
        if command["action"].startswith("browser_"):
            if incoming.session_id != command_payload.get("session_id"):
                connection.rollback()
                return False
            if incoming.action != "browser_close" and incoming.success and (
                incoming.page_identity is None
                or incoming.navigation_generation is None
                or not re.fullmatch(r"[a-f0-9-]{36}", incoming.page_identity)
                or incoming.navigation_generation < 1
            ):
                connection.rollback()
                return False
            if incoming.action != "browser_close" and incoming.page_identity is not None:
                session_state = connection.execute(
                    """
                    SELECT status, page_identity, navigation_generation
                    FROM browser_sessions
                    WHERE id = ? AND owner_id = ? AND device_id = ?
                    """,
                    (
                        incoming.session_id,
                        command["owner_id"],
                        device_id,
                    ),
                ).fetchone()
                if (
                    session_state is None
                    or session_state["status"] not in {"planned", "active"}
                    or (
                        session_state["page_identity"] is not None
                        and session_state["page_identity"] != incoming.page_identity
                    )
                    or (
                        session_state["navigation_generation"] is not None
                        and incoming.navigation_generation
                        < session_state["navigation_generation"]
                    )
                ):
                    connection.rollback()
                    return False
        elif incoming.session_id is not None:
            connection.rollback()
            return False
        if incoming.failure_code is not None and (
            incoming.action != "click" or incoming.success
        ):
            connection.rollback()
            return False

        stored_observation = None
        if incoming.observation is not None:
            if command["action"] != "browser_inspect_controls":
                connection.rollback()
                return False
            try:
                (
                    width,
                    height,
                    elements,
                    browser_session_id,
                    page_identity,
                    navigation_generation,
                ) = _validate_dom_observation(incoming.observation)
            except ValueError:
                connection.rollback()
                return False
            if (
                browser_session_id != command_payload.get("session_id")
                or incoming.page_identity != page_identity
                or incoming.navigation_generation != navigation_generation
            ):
                connection.rollback()
                return False
            stored_observation = _store_observation(
                connection,
                command["owner_id"],
                device_id,
                "browser_dom",
                width,
                height,
                "playwright-dom",
                elements,
                command_id=incoming.command_id,
                browser_session_id=browser_session_id,
                page_identity=page_identity,
                navigation_generation=navigation_generation,
            )
        if incoming.observation_id is not None:
            observation = connection.execute(
                """
                SELECT id FROM computer_observations
                WHERE id = ? AND owner_id = ? AND device_id = ? AND command_id = ?
                """,
                (
                    incoming.observation_id,
                    command["owner_id"],
                    device_id,
                    incoming.command_id,
                ),
            ).fetchone()
            if command["action"] != "capture_screen" or observation is None:
                connection.rollback()
                return False
            stored_observation = {"observation_id": incoming.observation_id}
        if command["action"] == "capture_screen" and incoming.success and stored_observation is None:
            connection.rollback()
            return False

        capability = connection.execute(
            """
            SELECT capabilities FROM agent_desktop_capabilities
            WHERE device_id = ? AND owner_id = ?
            """,
            (device_id, command["owner_id"]),
        ).fetchone()
        capability_allowed = False
        if capability is not None:
            try:
                reported = json.loads(capability["capabilities"])
                capability_allowed = (
                    _action_execution_enabled(incoming.action)
                    and (
                        reported.get("browser_automation") is True
                        if incoming.action.startswith("browser_")
                        else reported.get("desktop_execution") is True
                    )
                    and reported.get(incoming.action) is True
                )
            except (TypeError, json.JSONDecodeError):
                capability_allowed = False

        needs_verification = incoming.action == "browser_submit"
        verified = (
            not needs_verification
            or incoming.verification_status == "verified"
        )
        if incoming.success and capability_allowed and verified:
            status = "completed"
            result = {
                "command_id": incoming.command_id,
                "action": incoming.action,
                "success": True,
                "message": incoming.message,
            }
            audit_reason = "approved computer action completed"
        else:
            status = "failed"
            if not _action_execution_enabled(incoming.action):
                message = (
                    "Browser automation is disabled"
                    if incoming.action.startswith("browser_")
                    else "Desktop execution is disabled"
                )
                audit_reason = "computer execution disabled"
            elif not capability_allowed:
                message = "Computer capability was unavailable"
                audit_reason = "computer capability unavailable"
            elif needs_verification and not verified:
                message = (
                    "Submission may have occurred but was not verified; do not retry"
                )
                audit_reason = "external action was not verified; retry prohibited"
            else:
                message = incoming.message
                audit_reason = (
                    "desktop click rejected as stale; new approval required"
                    if incoming.failure_code == "desktop_click_stale"
                    else "computer action failed"
                )
            result = {
                "command_id": incoming.command_id,
                "action": incoming.action,
                "success": False,
                "message": message,
            }
        if incoming.failure_code is not None:
            result["failure_code"] = incoming.failure_code
        for key in ("session_id", "verification_status"):
            value = getattr(incoming, key)
            if value is not None:
                result[key] = value
        if incoming.page_identity is not None:
            result["page_identity"] = incoming.page_identity
        if incoming.navigation_generation is not None:
            result["navigation_generation"] = incoming.navigation_generation
        if incoming.session_closed is not None:
            result["session_closed"] = incoming.session_closed
        if stored_observation is not None:
            result["observation_id"] = stored_observation["observation_id"]
        if incoming.data is not None:
            result["data"] = incoming.data

        persisted_result = {
            key: value for key, value in result.items() if key != "data"
        }
        persisted_payload = (
            "{}" if incoming.action == "browser_fill" else command["payload"]
        )
        connection.execute(
            """
            UPDATE agent_commands SET status = ?, payload = ?, result = ?, updated_at = ?
            WHERE id = ? AND owner_id = ? AND device_id = ? AND status = 'executing'
            """,
            (
                status,
                persisted_payload,
                json.dumps(persisted_result),
                _utc_text(),
                incoming.command_id,
                command["owner_id"],
                device_id,
            ),
        )
        if command["proposal_id"]:
            connection.execute(
                """
                UPDATE computer_action_proposals SET status = ?, updated_at = ?
                    , verification_status = CASE
                    WHEN ? = 'desktop_click_stale' THEN 'failed'
                        WHEN ? = 'verified' THEN 'verified'
                        WHEN ? = 'failed' THEN 'failed'
                        ELSE verification_status
                    END
                WHERE id = ? AND owner_id = ?
                """,
                (
                    "completed" if status == "completed" else "failed",
                    _utc_text(),
                        incoming.failure_code,
                        incoming.verification_status,
                        incoming.verification_status,
                    command["proposal_id"],
                    command["owner_id"],
                ),
            )
        if incoming.session_id is not None:
            if incoming.action == "browser_close" or incoming.session_closed is True:
                connection.execute(
                    """
                    UPDATE browser_sessions
                    SET status = 'closed', initial_url = NULL,
                        page_identity = NULL, navigation_generation = NULL
                    WHERE id = ? AND owner_id = ? AND device_id = ?
                    """,
                    (incoming.session_id, command["owner_id"], device_id),
                )
                page_changed = True
            elif incoming.page_identity is not None and incoming.navigation_generation is not None:
                previous_page = connection.execute(
                    """
                    SELECT page_identity, navigation_generation
                    FROM browser_sessions
                    WHERE id = ? AND owner_id = ? AND device_id = ?
                    """,
                    (incoming.session_id, command["owner_id"], device_id),
                ).fetchone()
                if previous_page is None:
                    connection.rollback()
                    return False
                page_changed = (
                    previous_page["page_identity"] != incoming.page_identity
                    or previous_page["navigation_generation"]
                    != incoming.navigation_generation
                )
                connection.execute(
                    """
                    UPDATE browser_sessions
                    SET status = 'active', page_identity = ?,
                        navigation_generation = ?
                    WHERE id = ? AND owner_id = ? AND device_id = ?
                    """,
                    (
                        incoming.page_identity,
                        incoming.navigation_generation,
                        incoming.session_id,
                        command["owner_id"],
                        device_id,
                    ),
                )
            else:
                page_changed = False
            if page_changed:
                stale_proposals = connection.execute(
                    """
                    SELECT p.id, c.id AS command_id
                    FROM computer_action_proposals p
                    LEFT JOIN agent_commands c
                      ON c.proposal_id = p.id AND c.owner_id = p.owner_id
                    WHERE p.browser_session_id = ? AND p.owner_id = ?
                      AND p.status IN ('proposed', 'pending_approval', 'approved')
                    """,
                    (incoming.session_id, command["owner_id"]),
                ).fetchall()
                for stale in stale_proposals:
                    connection.execute(
                        """
                        UPDATE computer_action_proposals SET status = 'failed',
                            updated_at = ?
                        WHERE id = ? AND owner_id = ?
                        """,
                        (_utc_text(), stale["id"], command["owner_id"]),
                    )
                    if stale["command_id"] is not None:
                        connection.execute(
                            """
                            UPDATE agent_approvals SET status = 'cancelled',
                                consumed_at = ?
                            WHERE command_id = ? AND owner_id = ? AND status = 'pending'
                            """,
                            (_utc_text(), stale["command_id"], command["owner_id"]),
                        )
                        connection.execute(
                            """
                            UPDATE agent_commands SET status = 'cancelled',
                                updated_at = ?
                            WHERE id = ? AND owner_id = ?
                              AND status IN ('pending_approval', 'approved')
                            """,
                            (_utc_text(), stale["command_id"], command["owner_id"]),
                        )
        _agent_audit(
            connection,
            command["owner_id"],
            (
                "desktop_click_rejected_stale"
                if incoming.failure_code == "desktop_click_stale"
                else "command_completed" if status == "completed" else "command_failed"
            ),
            device_id=device_id,
            command_id=incoming.command_id,
            details={
                "reason": audit_reason,
                "agent_claimed_success": incoming.success,
                "verification_status": incoming.verification_status,
            },
        )
        connection.execute(
        """
        UPDATE agent_execution_sessions
        SET state = ?, ended_at = ?
        WHERE owner_id = ? AND device_id = ? AND command_id = ?
        """,
        (
            "completed" if status == "completed" else "failed",
            _utc_text(),
            command["owner_id"],
            device_id,
            incoming.command_id,
        ),
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
        connection = get_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT owner_id FROM agent_devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            if owner is None:
                connection.rollback()
                await websocket.close(code=1008, reason="Unknown device")
                return
            owner_id = owner["owner_id"]
            if not _consume_agent_rate_limit(
                connection,
                owner_id,
                "websocket_auth",
                AGENT_WS_AUTH_ATTEMPTS_PER_MINUTE,
            ):
                _agent_audit(
                    connection,
                    owner_id,
                    "rate_limit_exceeded",
                    device_id=device_id,
                    details={"scope": "websocket_auth"},
                )
                connection.commit()
                await websocket.close(code=1008, reason="Device authentication rate limit exceeded")
                return
            connection.commit()
        finally:
            connection.close()

        try:
            auth_message = parse_message(
                await asyncio.wait_for(websocket.receive_text(), timeout=10)
            )
        except ValueError:
            _audit_agent_protocol_failure(
                owner_id,
                device_id,
                "invalid or oversized authentication message",
            )
            await websocket.close(code=1008, reason="Invalid authentication message")
            return
        if (
            set(auth_message) != {"type", "credential"}
            or auth_message.get("type") != "authenticate"
            or not isinstance(auth_message.get("credential"), str)
            or not 32 <= len(auth_message["credential"]) <= 128
        ):
            _audit_agent_protocol_failure(
                owner_id,
                device_id,
                "invalid authentication message",
            )
            await websocket.close(code=1008, reason="Invalid authentication message")
            return

        connection = get_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            device = connection.execute(
                """
                SELECT owner_id, credential_hash, status
                FROM agent_devices WHERE id = ?
                """,
                (device_id,),
            ).fetchone()
            if device is None:
                connection.rollback()
                await websocket.close(code=1008, reason="Unknown device")
                return
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
            connection.execute(
                """
                UPDATE agent_devices SET status = 'connected', last_seen_at = ?
                WHERE id = ? AND owner_id = ? AND status != 'revoked'
                """,
                (_utc_text(), device_id, owner_id),
            )
            _agent_audit(connection, owner_id, "agent_connected", device_id=device_id)
            connection.commit()
        finally:
            connection.close()

        previous_connection = AGENT_CONNECTIONS.get(device_id)
        AGENT_CONNECTIONS[device_id] = websocket
        if previous_connection is not None:
            await previous_connection.close(code=1008, reason="Replaced by a new connection")
        await websocket.send_json({"type": "authenticated", "device_id": device_id})
        while True:
            raw_message = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=AGENT_HEARTBEAT_TIMEOUT_SECONDS,
            )
            connection = get_db()
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not _consume_agent_rate_limit(
                    connection,
                    owner_id,
                    "websocket",
                    AGENT_WS_MESSAGES_PER_MINUTE,
                ):
                    _agent_audit(
                        connection,
                        owner_id,
                        "rate_limit_exceeded",
                        device_id=device_id,
                        details={"scope": "websocket"},
                    )
                    connection.commit()
                    await websocket.close(code=1008, reason="WebSocket message rate limit exceeded")
                    return
                device_state = connection.execute(
                    "SELECT status FROM agent_devices WHERE id = ? AND owner_id = ?",
                    (device_id, owner_id),
                ).fetchone()
                if (
                    device_state is None
                    or device_state["status"] != "connected"
                    or _agent_safety_stopped(connection, owner_id)
                ):
                    connection.rollback()
                    await websocket.close(code=1008, reason="Device is no longer authorized")
                    return
                connection.execute(
                    "UPDATE agent_devices SET last_seen_at = ? WHERE id = ? AND owner_id = ?",
                    (_utc_text(), device_id, owner_id),
                )
                connection.commit()
            finally:
                connection.close()

            try:
                message = parse_message(raw_message)
            except ValueError:
                _audit_agent_protocol_failure(owner_id, device_id, "invalid or oversized JSON message")
                await websocket.close(code=1008, reason="Invalid WebSocket message")
                return

            if message.get("type") == "heartbeat" and set(message) == {"type"}:
                await websocket.send_json({"type": "heartbeat_ack"})
                continue

            if message.get("type") == "capabilities" and set(message) == {
                "type",
                "capabilities",
                "session_type",
            }:
                capabilities = message["capabilities"]
                session_type = message["session_type"]
                if (
                    not isinstance(capabilities, dict)
                    or not isinstance(session_type, str)
                    or len(session_type) > 32
                    or set(capabilities) - set(LOCAL_AGENT_ALLOWED_ACTIONS) - {
                        "desktop_execution",
                        "browser_automation",
                        "x11",
                        "wayland",
                    }
                ):
                    _audit_agent_protocol_failure(owner_id, device_id, "invalid capability message")
                    await websocket.close(code=1008, reason="Invalid capability message")
                    return
                if not all(isinstance(value, bool) for value in capabilities.values()):
                    _audit_agent_protocol_failure(owner_id, device_id, "invalid capability values")
                    await websocket.close(code=1008, reason="Invalid capability message")
                    return
                connection = get_db()
                try:
                    with connection:
                        connection.execute(
                            """
                            INSERT INTO agent_desktop_capabilities
                            (device_id, owner_id, capabilities, session_type, updated_at)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(device_id) DO UPDATE SET
                                owner_id = excluded.owner_id,
                                capabilities = excluded.capabilities,
                                session_type = excluded.session_type,
                                updated_at = excluded.updated_at
                            """,
                            (
                                device_id,
                                owner_id,
                                json.dumps(capabilities, sort_keys=True),
                                session_type,
                                _utc_text(),
                            ),
                        )
                finally:
                    connection.close()
                await websocket.send_json({
                    "type": "capabilities_ack",
                    "accepted": True,
                })
                continue

            try:
                incoming = AgentCommandResult(**message)
            except ValidationError:
                _audit_agent_protocol_failure(owner_id, device_id, "invalid command result")
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
    except asyncio.TimeoutError:
        try:
            await websocket.close(code=1001, reason="Local Agent heartbeat timed out")
        except (RuntimeError, WebSocketDisconnect):
            pass
    except (WebSocketDisconnect, RuntimeError):
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
            _fail_disconnected_agent_commands(device_id, owner_id)


def _audit_agent_protocol_failure(owner_id, device_id, reason):
    connection = get_db()
    try:
        with connection:
            _agent_audit(
                connection,
                owner_id,
                "agent_response_rejected",
                device_id=device_id,
                details={"reason": reason},
            )
    finally:
        connection.close()


def _fail_disconnected_agent_commands(device_id, owner_id):
    if owner_id is None:
        return
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT id, action FROM agent_commands
            WHERE owner_id = ? AND device_id = ? AND status = 'executing'
            """,
            (owner_id, device_id),
        ).fetchall()
        for row in rows:
            result = {
                "command_id": row["id"],
                "action": row["action"],
                "success": False,
                "message": "Local Agent disconnected before returning a result",
            }
            connection.execute(
                """
                UPDATE agent_commands SET status = 'failed', result = ?, updated_at = ?
                WHERE id = ? AND status = 'executing'
                """,
                (json.dumps(result), _utc_text(), row["id"]),
            )
            connection.execute(
                """
                UPDATE agent_execution_sessions
                SET state = 'failed', ended_at = ?
                WHERE owner_id = ? AND command_id = ?
                """,
                (_utc_text(), owner_id, row["id"]),
            )
            _agent_audit(
                connection,
                owner_id,
                "command_failed",
                device_id=device_id,
                command_id=row["id"],
                details={"reason": "Local Agent disconnected"},
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    for row in rows:
        future = PENDING_AGENT_COMMANDS.get(row["id"])
        if future is not None and not future.done():
            future.set_result({
                "command_id": row["id"],
                "action": row["action"],
                "success": False,
                "message": "Local Agent disconnected before returning a result",
            })


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
