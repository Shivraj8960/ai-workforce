from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, List


app = FastAPI(
    title="AI Workforce Platform",
    version="0.2.0",
    description="AI workforce task planning and execution API"
)


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
    dependencies: List[str] = []
    result: Optional[str] = None


# =========================
# In-memory storage
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

tasks_db = []


# =========================
# Basic endpoints
# =========================

@app.get("/")
def home():
    return {
        "message": "AI Workforce Platform is running",
        "version": "0.2.0"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "employees": len(employees_db),
        "tasks": len(tasks_db)
    }


# =========================
# Employee endpoints
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

        tasks_db.append(task)
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
# Task endpoints
# =========================

@app.get("/tasks")
def get_tasks():
    return {
        "total_tasks": len(tasks_db),
        "tasks": tasks_db
    }


@app.get("/tasks/{task_id}")
def get_task(task_id: str):

    for task in tasks_db:
        if task.id == task_id:
            return task

    return {
        "error": "Task not found",
        "task_id": task_id
    }


@app.post("/tasks/{task_id}/execute")
def execute_task(task_id: str):

    for task in tasks_db:

        if task.id == task_id:

            # Check dependencies
            for dependency_id in task.dependencies:

                dependency = next(
                    (item for item in tasks_db if item.id == dependency_id),
                    None
                )

                if dependency and dependency.status != "completed":
                    return {
                        "message": "Task is waiting for dependency",
                        "task": task,
                        "blocked_by": dependency_id
                    }

            task.status = "running"

            # Phase 1 simulation:
            # Real AI execution will be added in a later phase.
            task.result = (
                f"{task.assigned_to} executed task: {task.title}"
            )

            task.status = "completed"

            return {
                "message": "Task executed successfully",
                "task": task,
                "result": task.result
            }

    return {
        "error": "Task not found",
        "task_id": task_id
    }


# =========================
# Workforce status
# =========================

@app.get("/workforce")
def workforce_status():

    pending = sum(1 for task in tasks_db if task.status == "pending")
    running = sum(1 for task in tasks_db if task.status == "running")
    completed = sum(1 for task in tasks_db if task.status == "completed")

    return {
        "employees": len(employees_db),
        "tasks": {
            "total": len(tasks_db),
            "pending": pending,
            "running": running,
            "completed": completed
        }
    }
