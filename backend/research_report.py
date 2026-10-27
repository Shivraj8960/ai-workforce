"""Research-to-report workflow helpers for NEX AI."""

from datetime import datetime, timezone
from typing import Any


def build_research_report(
    goal: str,
    research_findings: list[dict[str, Any]],
    analysis: str,
    recommendations: list[str] | None = None,
) -> dict[str, Any]:
    """Build a structured report draft from supplied research and analysis."""
    clean_goal = goal.strip()
    if not clean_goal:
        raise ValueError("Research goal cannot be empty.")

    if not isinstance(research_findings, list):
        raise ValueError("Research findings must be a list.")

    if not analysis.strip():
        raise ValueError("Analysis cannot be empty.")

    findings = []
    for item in research_findings:
        if not isinstance(item, dict):
            raise ValueError("Each research finding must be an object.")

        finding = str(item.get("finding", "")).strip()
        source = str(item.get("source", "")).strip()
        if finding:
            findings.append(
                {
                    "finding": finding,
                    "source": source or None,
                    "verified": bool(item.get("verified", False)),
                }
            )

    return {
        "title": f"Research Report: {clean_goal}",
        "goal": clean_goal,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "draft",
        "executive_summary": (
            "Draft summary pending human review. "
            "Validate the findings and analysis before relying on this report."
        ),
        "findings": findings,
        "analysis": analysis.strip(),
        "recommendations": [
            str(item).strip()
            for item in (recommendations or [])
            if str(item).strip()
        ],
        "review_required": True,
        "limitations": [
            "Unverified findings must not be treated as established facts.",
            "Source accessibility alone does not establish factual accuracy.",
        ],
    }


def build_research_task_plan(goal: str) -> list[dict[str, object]]:
    """Create a dependency-ordered research-to-report task plan."""
    clean_goal = goal.strip() if isinstance(goal, str) else ""
    if not clean_goal:
        raise ValueError("Research goal cannot be empty.")
    return [
        {"title": "Research the goal", "description": f"Research: {clean_goal}. Collect findings and source references; distinguish verified facts from unverified claims.", "assigned_to": "Researcher", "dependencies": []},
        {"title": "Analyze research findings", "description": f"Analyze findings for: {clean_goal}. Identify patterns, limitations, and evidence gaps.", "assigned_to": "Analyst", "dependencies": [1]},
        {"title": "Draft the research report", "description": f"Draft a structured report for: {clean_goal}. Include findings, sources, limitations, recommendations, and require human review.", "assigned_to": "Writer", "dependencies": [1, 2]},
    ]
