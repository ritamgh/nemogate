"""A tool the rules file maps under `tools:` has its path argument read fail closed: a list of
paths is checked item by item, and a missing or unreadable argument is blocked (not skipped) when a
path_forbidden rule covers the tool's operation."""

import asyncio
import json
from contextlib import contextmanager

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.prebuilt.tool_node import ToolCallRequest

from nemogate.gate.guard import Guard

RULES = """\
version: 1
tools:
  bulk_rm: {path_arg: paths, op: delete}
  save_note: {path_arg: file, op: write}
rules:
  - id: keep-migrations
    text: Keep the migrations folder.
    check:
      path_forbidden: {glob: "migrations/**", ops: [delete]}
"""
KEEP = "Blocked by rule keep-migrations: Keep the migrations folder."


class Tool:
    def __init__(self):
        self.ran = []

    def __call__(self, request):
        call = request.tool_call
        self.ran.append(call["args"])
        return ToolMessage(
            content="ran", tool_call_id=call["id"], name=call["name"], status="success"
        )

    async def acall(self, request):
        return self(request)


def req(name, args, call_id="c1"):
    call = {"name": name, "args": args, "id": call_id, "type": "tool_call"}
    return ToolCallRequest(tool_call=call, tool=None, state={}, runtime=None)


@contextmanager
def thread():
    config = {"configurable": {"thread_id": "t1"}, "metadata": {"lc_agent_name": "coder"}}
    token = var_child_runnable_config.set(config)
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "migrations").mkdir()
    monkeypatch.chdir(root)
    return root


def make(tmp_path, text=RULES, **kwargs):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    (mw,) = Guard.from_yaml(path, **kwargs).middleware()
    return mw


def call(mw, name, args):
    tool = Tool()
    with thread():
        result = mw.wrap_tool_call(req(name, args), tool)
    return result.content, tool.ran


def test_a_list_with_one_forbidden_item_is_blocked_and_the_tool_does_not_run(tmp_path, repo):
    # Breaks if a list-valued path argument is skipped: the tool deletes migrations/a.sql.
    mw = make(tmp_path)
    paths = ["app/x.py", "migrations/a.sql"]
    assert call(mw, "bulk_rm", {"paths": paths}) == (KEEP, [])


def test_a_list_of_only_allowed_paths_runs(tmp_path, repo):
    # Breaks if every list is blocked: a list with nothing forbidden must still run.
    mw = make(tmp_path)
    args = {"paths": ["app/x.py", "/app/y.py"]}
    assert call(mw, "bulk_rm", args) == ("ran", [args])


def test_a_list_with_a_forbidden_virtual_path_is_blocked(tmp_path, repo):
    # Breaks if list items skip the Deep Agents virtual-path reading ("/migrations/a.sql").
    mw = make(tmp_path)
    assert call(mw, "bulk_rm", {"paths": ["/migrations/a.sql"]}) == (KEEP, [])


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        ({}, "argument paths is missing"),
        ({"paths": None}, "argument paths is not a path or list of paths"),
        ({"paths": 7}, "argument paths is not a path or list of paths"),
        ({"paths": {"a": 1}}, "argument paths is not a path or list of paths"),
        ({"paths": ["app/x.py", 3]}, "argument paths is not a path or list of paths"),
        ({"paths": [["migrations/a.sql"]]}, "argument paths is not a path or list of paths"),
    ],
)
def test_a_missing_or_unreadable_path_argument_is_blocked_with_the_reason(
    tmp_path, repo, args, problem
):
    # Breaks if a mapped tool whose path cannot be read is skipped instead of blocked.
    mw = make(tmp_path)
    assert call(mw, "bulk_rm", args) == (f"{KEEP} (tool bulk_rm: {problem})", [])


def test_observe_mode_logs_a_would_block_and_runs_the_tool(tmp_path, repo):
    # Breaks if the fail-closed cases ignore the rule's mode and block an observe-mode rule.
    trace = tmp_path / "trace.jsonl"
    text = RULES.replace("    text: Keep", "    mode: observe\n    text: Keep")
    mw = make(tmp_path, text, trace_path=trace)
    assert call(mw, "bulk_rm", {"paths": 7}) == ("ran", [{"paths": 7}])
    assert call(mw, "bulk_rm", {"paths": ["migrations/a.sql"]})[0] == "ran"
    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    reasons = [x["reason"] for x in lines if x["type"] == "gate_block"]
    assert reasons == [
        "would block (observe mode): Keep the migrations folder. "
        "(tool bulk_rm: argument paths is not a path or list of paths)",
        "would block (observe mode): Keep the migrations folder.",
    ]


def test_a_mapped_tool_no_rule_covers_runs_without_its_path_argument(tmp_path, repo):
    # Breaks if every mapped tool is blocked on a bad argument: save_note is a write tool and
    # the only rule covers delete, so a missing file argument is nobody's business.
    mw = make(tmp_path)
    assert call(mw, "save_note", {}) == ("ran", [{}])
    assert call(mw, "save_note", {"file": None}) == ("ran", [{"file": None}])


def test_a_tool_that_is_not_mapped_runs_without_a_path_argument(tmp_path, repo):
    mw = make(tmp_path)
    assert call(mw, "lookup", {}) == ("ran", [{}])


def test_the_async_path_blocks_a_list_and_a_missing_argument_too(tmp_path, repo):
    mw = make(tmp_path)
    tool = Tool()

    async def scenario():
        with thread():
            listed = await mw.awrap_tool_call(
                req("bulk_rm", {"paths": ["migrations/a.sql"]}), tool.acall
            )
            missing = await mw.awrap_tool_call(req("bulk_rm", {}, "c2"), tool.acall)
        return listed.content, missing.content

    assert asyncio.run(scenario()) == (KEEP, f"{KEEP} (tool bulk_rm: argument paths is missing)")
    assert tool.ran == []
