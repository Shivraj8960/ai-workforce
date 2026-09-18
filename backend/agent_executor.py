import os
from typing import Dict

from dotenv import load_dotenv
from openai import OpenAI


# Always load the .env file located beside this Python file.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


AGENT_CONFIG = {
    "researcher": {
        "role": "Researcher",
        "instruction": (
            "Investigate the task systematically. Gather relevant facts, "
            "opportunities, assumptions, and useful evidence. Separate known "
            "facts from assumptions and identify information that is still missing."
        ),
        "output": (
            "Return a concise research report with key findings, useful evidence, "
            "assumptions, gaps, and actionable next steps."
        ),
    },
    "analyst": {
        "role": "Analyst",
        "instruction": (
            "Analyze the available task information and research. Identify "
            "patterns, relationships, risks, trade-offs, priorities, and "
            "practical conclusions. Do not invent missing data."
        ),
        "output": (
            "Return structured analysis with findings, reasoning, risks, "
            "priorities, and clear conclusions."
        ),
    },
    "writer": {
        "role": "Writer",
        "instruction": (
            "Transform the available information into clear, professional, "
            "well-structured content appropriate for the requested task. "
            "Preserve important facts and avoid unsupported claims."
        ),
        "output": (
            "Return a polished deliverable using headings, bullets, tables, "
            "or other formatting when useful."
        ),
    },
    "designer": {
        "role": "Designer",
        "instruction": (
            "Create clear design concepts, wireframes, layouts, and user experience "
            "recommendations based on the task requirements. Focus on usability, "
            "visual hierarchy, consistency, and practical implementation."
        ),
        "output": (
            "Return a structured design specification containing layout ideas, "
            "key components, user flow, visual guidance, and implementation notes."
        ),
    },
    "reviewer": {
        "role": "Reviewer",
        "instruction": (
            "Critically inspect the available work for accuracy, completeness, "
            "clarity, consistency, risks, and missing requirements. Identify "
            "specific issues and explain how each should be improved."
        ),
        "output": (
            "Return a review containing strengths, issues, missing items, "
            "recommended corrections, and a final readiness assessment."
        ),
    },
    "executor": {
        "role": "Executor",
        "instruction": (
            "Work only from approved requirements. Convert the task into "
            "concrete execution steps, expected outputs, dependencies, and "
            "verification checks. Do not claim an external action was performed "
            "when the system cannot actually perform it."
        ),
        "output": (
            "Return an execution result containing actions, expected outputs, "
            "verification status, and any blockers."
        ),
    },
}


def build_prompt(config, task_title, task_description, previous_context=""):
    return f"""
You are the {config['role']} in an Nex AI system.

Your role:
{config['instruction']}

Task title:
{task_title}

Task description:
{task_description}

Previous agent context:
{previous_context if previous_context else "None"}

Required output:
{config['output']}

General rules:
- Stay focused on the assigned task.
- Be clear, structured, and practical.
- Do not invent facts, sources, actions, or results.
- Clearly identify assumptions or missing information.
- Produce an output that can be passed to the next employee in the workflow.
"""


def execute_with_mercury(prompt):
    api_key = os.getenv("INCEPTION_API_KEY")

    if not api_key:
        raise RuntimeError("INCEPTION_API_KEY is not configured.")

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.inceptionlabs.ai/v1",
    )

    response = client.chat.completions.create(
        model="mercury-2",
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
        max_tokens=1000,
        reasoning_effort="instant",
    )

    content = response.choices[0].message.content

    if not content:
        raise RuntimeError("Mercury 2 returned an empty response.")

    return content


def execute_with_ollama(prompt):
    client = OpenAI(
        api_key="ollama",
        base_url="http://localhost:11434/v1",
    )

    response = client.chat.completions.create(
        model="llama3.2:3b",
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
        max_tokens=1000,
    )

    content = response.choices[0].message.content

    if not content:
        raise RuntimeError("Ollama returned an empty response.")

    return content


def generate_plan(goal):
    prompt = f"""You are the planning engine of an Nex AI system.

User goal:
{goal}

Create a practical workflow of 3 to 7 tasks. Return ONLY valid JSON as a list. Each task must contain exactly these fields:
- title: short task name
- description: clear task description
- assigned_to: one of Researcher, Analyst, Writer, Reviewer, Executor
- dependencies: list of previous task numbers, using integers starting from 1

Example format:
[{{"title":"Research market","description":"Identify relevant market information","assigned_to":"Researcher","dependencies":[]}}]

Do not include markdown or explanations."""
    try:
        result = execute_with_mercury(prompt)
        return result
    except Exception as mercury_error:
        try:
            return execute_with_ollama(prompt)
        except Exception as ollama_error:
            raise RuntimeError(f"Both planning providers failed. Mercury: {mercury_error}; Ollama: {ollama_error}")


def execute_with_gemini(prompt):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    client = OpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )

    response = client.chat.completions.create(
        model="gemini-3.8-flash",
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
        max_tokens=1000,
    )

    content = response.choices[0].message.content

    if not content:
        raise RuntimeError("Gemini returned an empty response.")

    return content


def execute_agent(
    agent: str,
    task_title: str,
    task_description: str,
    previous_context: str = "",
) -> Dict:

    agent_key = agent.lower().strip()
    config = AGENT_CONFIG.get(agent_key)

    if not config:
        return {
            "agent": agent_key,
            "task": task_title,
            "status": "failed",
            "provider": None,
            "result": f"Unknown agent: {agent}",
        }

    prompt = build_prompt(
        config,
        task_title,
        task_description,
        previous_context,
    )

    # Provider 1: Mercury 2
    try:
        result = execute_with_mercury(prompt)
        return {
            "agent": agent_key,
            "task": task_title,
            "status": "completed",
            "provider": "inception-mercury-2",
            "result": result,
        }
    except Exception as mercury_error:
        pass

    # Provider 2: Gemini
    try:
        result = execute_with_gemini(prompt)
        return {
            "agent": agent_key,
            "task": task_title,
            "status": "completed",
            "provider": "google-gemini-3.8-flash",
            "result": result,
        }
    except Exception as gemini_error:
        pass

    # Provider 3: Ollama
    try:
        result = execute_with_ollama(prompt)
        return {
            "agent": agent_key,
            "task": task_title,
            "status": "completed",
            "provider": "ollama-llama3.2:3b",
            "result": result,
        }
    except Exception as ollama_error:
        return {
            "agent": agent_key,
            "task": task_title,
            "status": "failed",
            "provider": None,
            "result": (
                "All AI providers failed.\n"
                f"Mercury 2 error: {mercury_error}\n"
                f"Gemini error: {gemini_error}\n"
                f"Ollama error: {ollama_error}"
            ),
        }

