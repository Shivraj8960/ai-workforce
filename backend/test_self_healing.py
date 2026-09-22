from self_healing import execute_with_retries

calls = []

def test_executor(*args):
    calls.append(1)
    if len(calls) == 1:
        return {"status": "failed", "result": "timeout"}
    return {"status": "completed", "result": "recovered"}

result = execute_with_retries(
    test_executor,
    "Researcher",
    "Nex AI Self-Healing Test",
    "Test retry"
)

print("RESULT:", result)
print("CALLS:", len(calls))
