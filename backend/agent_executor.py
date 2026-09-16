from typing import Dict


def execute_agent(agent: str, task_title: str, task_description: str) -> Dict:
    """
    Execute a task using the assigned AI workforce agent.
    """

    agent = agent.lower().strip()

    agent_actions = {
        "researcher": "Researching information and identifying relevant opportunities.",
        "analyst": "Analyzing information and evaluating the task requirements.",
        "writer": "Creating and preparing written content.",
        "reviewer": "Reviewing the task output for quality and completeness.",
        "executor": "Executing the approved operational task.",
    }

    action = agent_actions.get(
        agent,
        "Processing the task using the assigned workforce agent."
    )

    result = (
        f"{action} "
        f"Task: {task_title}. "
        f"Description: {task_description}"
    )

    return {
        "agent": agent,
        "task": task_title,
        "status": "completed",
        "result": result,
    }
