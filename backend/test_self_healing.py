from self_healing import execute_with_retries


def test_retries_after_failure_then_succeeds():
    calls = []

    def mock_executor(agent, title, description, previous_context):
        calls.append(previous_context)
        if len(calls) == 1:
            return {"status": "failed", "result": "timeout"}
        return {"status": "completed", "result": "mocked recovery"}

    result = execute_with_retries(
        mock_executor,
        "Researcher",
        "Self-healing regression test",
        "Test retry behavior",
    )

    assert result["status"] == "completed"
    assert result["result"] == "mocked recovery"
    assert result["attempts"] == 2
    assert len(calls) == 2
    assert "Self-Healing Recovery Context" in calls[1]
    print("PASS: retry and recovery behavior")


if __name__ == "__main__":
    test_retries_after_failure_then_succeeds()
    print("ALL SELF-HEALING TESTS PASSED")
