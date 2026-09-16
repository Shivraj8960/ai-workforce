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
            "Research the task carefully. Identify useful information, "
            "opportunities, facts, and relevant findings."
        ),
    },
    "analyst": {
        "role": "Analyst",
        "instruction": (
            "Analyze the task and available information. Identify patterns, "
            "important findings, risks, and useful conclusions."
        ),
    },
    "writer": {
        "role": "Writer",
        "instruction": (
            "Create clear, professional, and useful written content "
            "based on the task requirements."
        ),
    },
    "reviewer": {
        "role": "Reviewer",
        "instruction": (
            "Review the task carefully for quality, accuracy, completeness, "
            "and potential improvements."
        ),
    },
    "executor": {
        "role": "Executor",
        "instruction": (
            "Execute the approved operational task according to the "
            "requirements and workflow."
        ),
    },
}


def build_prompt(config, task_title, task_description):
    return f"""
You are the {config['role']} in an AI Workforce system.

Your role:
{config['instruction']}

Task title:
{task_title}

Task description:
{task_description}

Perform the task and provide a useful result.
Be clear, structured, and practical.
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


def execute_agent(
    agent: str,
    task_title: str,
    task_description: str,
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
    )

    # Primary AI provider
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

        # Local fallback AI provider
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
                    "Both AI providers failed.\n"
                    f"Mercury 2 error: {mercury_error}\n"
                    f"Ollama error: {ollama_error}"
                ),
            }
