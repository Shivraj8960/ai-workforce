from typing import Dict


MAX_RETRIES = 2


def analyze_failure(execution: Dict) -> Dict:
    result = execution.get("result") or ""
    error_text = result.lower()

    if "timeout" in error_text or "timed out" in error_text:
        failure_type = "timeout"
        recovery = "retry"
    elif "429" in error_text or "rate limit" in error_text:
        failure_type = "rate_limit"
        recovery = "retry"
    elif "api key" in error_text or "authentication" in error_text:
        failure_type = "authentication"
        recovery = "fallback_provider"
    elif "provider" in error_text:
        failure_type = "provider_error"
        recovery = "fallback_provider"
    elif "connection" in error_text or "network" in error_text or "connection reset" in error_text:
        failure_type = "network_error"
        recovery = "retry"
    elif "500" in error_text or "502" in error_text or "503" in error_text or "504" in error_text or "server error" in error_text:
        failure_type = "server_error"
        recovery = "retry"
    elif "validation" in error_text or "invalid request" in error_text or "bad request" in error_text:
        failure_type = "validation_error"
        recovery = "stop"
    elif "dependency" in error_text or "not found" in error_text:
        failure_type = "dependency_error"
        recovery = "stop"
    else:
        failure_type = "unknown"
        recovery = "retry"

    return {
        "failure_type": failure_type,
        "recovery": recovery,
        "retryable": recovery == "retry",
    }


def should_retry(attempt: int) -> bool:
    return attempt < MAX_RETRIES


def execute_with_retries(execute_fn, agent, task_title, task_description, previous_context=""):
    attempts = 0
    last_execution = None
    failure_analysis = None

    while True:
        attempts += 1

        execution = execute_fn(
            agent,
            task_title,
            task_description,
            previous_context,
        )

        if execution.get("status") == "completed":
            execution["attempts"] = attempts
            execution["failure_analysis"] = failure_analysis
            execution["recovery"] = failure_analysis.get("recovery")
            execution["retryable"] = failure_analysis.get("retryable")
            return execution

        last_execution = execution
        failure_analysis = analyze_failure(execution)

        if not should_retry(attempts):
            execution["attempts"] = attempts
            execution["failure_analysis"] = failure_analysis
            execution["recovery"] = failure_analysis.get("recovery")
            execution["retryable"] = failure_analysis.get("retryable")
            return execution

        previous_context = (
            previous_context
            + "\n\nSelf-Healing Recovery Context:\n"
            + f"Previous attempt failed with {failure_analysis['failure_type']}."
            + " Retry the task carefully and produce a complete result."
        )
