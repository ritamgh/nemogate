from ledger.recall import load_cases
from refapp import recall_cases
from refapp.tasks import load_tasks


def test_one_case_per_task_with_its_ledger_check(tmp_path):
    out = tmp_path / "reference.yaml"
    assert recall_cases.main(["--out", str(out)]) == 0
    agents, tools, cases = load_cases(out)
    tasks = load_tasks()
    assert [c.name.split()[0] for c in cases] == [t.id for t in tasks]
    assert all(c.expect is not None for c in cases)
    assert agents == ["planner", "coder", "reviewer"]
    # Every tool an expected check names is one the agents have, or recall could never hit it.
    named = {c.expect.model_dump().get(k) for c in cases for k in ("name", "tool")} - {None}
    assert named <= set(tools)


def test_the_tool_list_is_the_one_the_earlier_recall_runs_used():
    # The 2/8 and later reference measurements used this list (FACTS §6); same order.
    assert recall_cases.tools() == [
        "ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep",
        "run_tests", "request_approval", "http_get",
    ]  # fmt: skip
