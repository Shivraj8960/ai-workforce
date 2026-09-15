from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="AI Workforce Platform")


class Employee(BaseModel):
    name: str
    role: str
    goal: str


class PlanRequest(BaseModel):
    goal: str


class Task(BaseModel):
    id: str
    title: str
    status: str = "pending"
    priority: str = "medium"


tasks_db = []


@app.get("/")
def home():
    return {
        "message": "AI Workforce Platform is running"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }


@app.post("/employees")
def create_employee(employee: Employee):
    return {
        "message": "AI Employee created successfully",
        "employee": {
            "name": employee.name,
            "role": employee.role,
            "goal": employee.goal,
            "status": "idle"
        }
    }


@app.post("/plan")
def create_plan(request: PlanRequest):

    goal = request.goal.lower()

    if "sales" in goal or "meeting" in goal or "customer" in goal:
        task_titles = [
            "Find potential customers",
            "Research and qualify prospects",
            "Prepare personalized outreach",
            "Request human approval",
            "Send approved outreach",
            "Track responses",
            "Update results"
        ]
    else:
        task_titles = [
            "Understand the goal",
            "Break the goal into smaller tasks",
            "Execute the required tasks",
            "Verify the results",
            "Report the outcome"
        ]

    created_tasks = []

    for index, title in enumerate(task_titles, start=1):
        task = Task(
            id=f"TASK-{index:03d}",
            title=title,
            status="pending",
            priority="medium"
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


@app.get("/tasks")
def get_tasks():
    return {
        "total_tasks": len(tasks_db),
        "tasks": tasks_db
    }

@app.post("/tasks/{task_id}/execute")
def execute_task(task_id: str):
    for task in tasks_db:
        if task["id"] == task_id:
            task["status"] = "running"

            task["status"] = "completed"

            return {
                "message": "Task executed successfully",
                "task": task,
                "result": f"Executed task: {task['title']}"
            }

    return {
        "error": "Task not found",
        "task_id": task_id
    }

