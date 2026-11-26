"""Guard.middleware(): block before the call, retry policy, approvals, re-pin, path
normalisation and the trace, driven through real ToolCallRequest objects (no model, no graph)."""

import asyncio
import json
import os
from contextlib import contextmanager

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.prebuilt.tool_node import ToolCallRequest

import nemogate.common.llm
from nemogate import gate
from nemogate.gate.errors import GuardLoopError
from nemogate.gate.guard import Guard
from nemogate.gate.middleware import GateMiddleware, glob_may_match_below, repin
from nemogate.gate.trace import TraceWriter
from nemogate.schemas.ledger import Constraint, Ledger
from nemogate.schemas.run import RunConfig

RULES = """\
version: 1
approval_tool: ask_human
tools:
  save_note: {path_arg: file, op: write}
rules:
  - id: no-migrations
    text: Never touch the migrations folder.
    check:
      path_forbidden: {glob: "migrations/**", ops: [write, delete]}
  - id: no-shell
    text: Do not run shell commands.
    check:
      tool_forbidden: {name: run_shell}
  - id: ask-before-delete
    text: Ask a human before deleting.
    check:
      approval_before: {tool: drop_table}
"""
OBSERVE = RULES.replace(
    "  - id: no-shell\n    text: Do not run shell commands.\n",
    "  - id: no-shell\n    text: Do not run shell commands.\n    mode: observe\n",
)
BLOCK_MIGRATIONS = "Blocked by rule no-migrations: Never touch the migrations folder."


class Tool:
    """A stand-in tool: records how often the handler (the real tool) actually ran."""

    def __init__(self, status="success"):
        self.ran = []
        self.status = status

    def __call__(self, request):
        self.ran.append(request.tool_call["args"])
        call = request.tool_call
        return ToolMessage(
            content="ran", tool_call_id=call["id"], name=call["name"], status=self.status
        )

    async def acall(self, request):
        return self(request)


def req(name, args=None, call_id="c1"):
    call = {"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}
    return ToolCallRequest(tool_call=call, tool=None, state={}, runtime=None)


@contextmanager
def thread(thread_id="t1", agent="coder"):
    config = {"configurable": {}, "metadata": {"lc_agent_name": agent}}
    if thread_id is not None:
        config["configurable"]["thread_id"] = thread_id
    token = var_child_runnable_config.set(config)
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo with app/ and migrations/ folders; the guard resolves paths against the cwd."""
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "migrations").mkdir()
    monkeypatch.chdir(root)
    return root


def make(tmp_path, text=RULES, **kwargs):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    guard = Guard.from_yaml(path, **kwargs)
    (mw,) = guard.middleware()
    return guard, mw


def run(mw, tool, name, args, call_id="c1"):
    return mw.wrap_tool_call(req(name, args, call_id), tool)


def test_middleware_is_a_list_with_one_agent_middleware(tmp_path):
    guard = Guard.from_yaml(_write(tmp_path, RULES))
    middleware = guard.middleware()
    assert [type(m) for m in middleware] == [type(middleware[0])]
    assert isinstance(middleware[0], AgentMiddleware)


def _write(tmp_path, text):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# Blocking before the call


def test_a_forbidden_write_is_blocked_and_the_tool_runs_zero_times(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        result = run(mw, tool, "write_file", {"file_path": "migrations/001.sql", "content": "x"})
    assert tool.ran == []
    assert isinstance(result, ToolMessage)
    assert (result.content, result.status, result.tool_call_id) == (BLOCK_MIGRATIONS, "error", "c1")


def test_an_allowed_write_runs_the_tool_and_returns_its_result(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        result = run(mw, tool, "write_file", {"file_path": "app/main.py", "content": "x"})
    assert tool.ran == [{"file_path": "app/main.py", "content": "x"}]
    assert result.content == "ran"


def test_a_forbidden_tool_is_blocked(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        result = run(mw, tool, "run_shell", {"cmd": "ls"})
    assert tool.ran == []
    assert result.content == "Blocked by rule no-shell: Do not run shell commands."


def test_dot_dot_traversal_into_the_forbidden_folder_is_blocked(tmp_path, repo):
    # Breaks if paths are matched as written: app/../migrations/x.sql does not look forbidden.
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        result = run(mw, tool, "write_file", {"file_path": "app/../migrations/x.sql"})
    assert tool.ran == []
    assert result.content == BLOCK_MIGRATIONS


def test_an_absolute_alias_of_the_forbidden_file_is_blocked(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        result = run(mw, tool, "edit_file", {"file_path": str(repo / "migrations" / "x.sql")})
    assert tool.ran == []
    assert result.content == BLOCK_MIGRATIONS


def test_a_symlink_into_the_forbidden_folder_is_blocked(tmp_path, repo):
    # Breaks if symlinks are not resolved: docs/ is migrations/ under another name.
    os.symlink(repo / "migrations", repo / "docs")
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        result = run(mw, tool, "write_file", {"file_path": "docs/x.sql"})
    assert tool.ran == []
    assert result.content == BLOCK_MIGRATIONS


def test_a_backslash_path_is_blocked_like_the_file_tools_read_it(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        result = run(mw, tool, "write_file", {"file_path": "app\\..\\migrations\\x.sql"})
    assert tool.ran == []
    assert result.content == BLOCK_MIGRATIONS


def test_a_path_that_cannot_be_resolved_is_matched_as_written(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        result = run(mw, tool, "write_file", {"file_path": "migrations/\x00x.sql"})
    assert tool.ran == []
    assert result.content == BLOCK_MIGRATIONS


def test_a_path_outside_the_repo_is_not_taken_for_a_forbidden_one(tmp_path, repo):
    # Breaks if ../ escapes are matched by their tail: ../other/migrations/x is not our folder.
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        run(mw, tool, "write_file", {"file_path": "../other/migrations/x.sql"})
    assert len(tool.ran) == 1


def test_a_custom_file_tool_from_the_tools_map_is_treated_as_a_file_tool(tmp_path, repo):
    # Breaks if only the built-in write_file/edit_file/delete names are known.
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        blocked = run(mw, tool, "save_note", {"file": "app/../migrations/n.md", "text": "hi"})
        allowed = run(mw, tool, "save_note", {"file": "app/n.md", "text": "hi"}, "c2")
    assert tool.ran == [{"file": "app/n.md", "text": "hi"}]
    assert blocked.content == BLOCK_MIGRATIONS
    assert allowed.content == "ran"


# Retry policy


def test_the_same_call_blocked_three_times_raises_and_never_runs_the_tool(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    args = {"file_path": "migrations/001.sql"}
    with thread():
        first = run(mw, tool, "write_file", args)
        second = run(mw, tool, "write_file", args)
        with pytest.raises(GuardLoopError, match="no-migrations") as raised:
            run(mw, tool, "write_file", args)
    assert (first.content, second.content) == (BLOCK_MIGRATIONS, BLOCK_MIGRATIONS)
    assert (raised.value.rule_id, raised.value.tool, raised.value.thread_id) == (
        "no-migrations",
        "write_file",
        "t1",
    )
    assert tool.ran == []


def test_a_changed_call_starts_again_at_count_one(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    a, b = {"file_path": "migrations/a.sql"}, {"file_path": "migrations/b.sql"}
    with thread():
        run(mw, tool, "write_file", a)
        run(mw, tool, "write_file", a)
        assert run(mw, tool, "write_file", b).content == BLOCK_MIGRATIONS  # b is at count 1
        with pytest.raises(GuardLoopError):
            run(mw, tool, "write_file", a)  # a is at count 3


def test_two_threads_count_independently(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    args = {"file_path": "migrations/001.sql"}
    with thread("t1"):
        run(mw, tool, "write_file", args)
        run(mw, tool, "write_file", args)
    with thread("t2"):
        assert run(mw, tool, "write_file", args).content == BLOCK_MIGRATIONS  # t2 at count 1
        assert run(mw, tool, "write_file", args).content == BLOCK_MIGRATIONS
    with thread("t1"), pytest.raises(GuardLoopError):
        run(mw, tool, "write_file", args)


def test_a_subagent_in_the_same_thread_shares_the_count(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    args = {"file_path": "migrations/001.sql"}
    with thread("t1", agent="coder"):
        run(mw, tool, "write_file", args)
        run(mw, tool, "write_file", args)
    with thread("t1", agent="reviewer"), pytest.raises(GuardLoopError):
        run(mw, tool, "write_file", args)


def test_runs_without_a_thread_id_share_the_default_thread(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    args = {"file_path": "migrations/001.sql"}
    with thread(None):
        run(mw, tool, "write_file", args)
    with thread(None):
        run(mw, tool, "write_file", args)
    with thread(None), pytest.raises(GuardLoopError) as raised:
        run(mw, tool, "write_file", args)
    assert raised.value.thread_id == "default"


def test_a_call_outside_any_run_config_uses_the_default_thread(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    result = run(mw, tool, "write_file", {"file_path": "migrations/001.sql"})
    assert result.content == BLOCK_MIGRATIONS


# Approvals


def test_approval_before_blocks_until_the_approval_tool_succeeds(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        blocked = run(mw, tool, "drop_table", {"name": "users"})
        run(mw, tool, "ask_human", {"question": "ok?"})
        allowed = run(mw, tool, "drop_table", {"name": "users"}, "c3")
    assert blocked.content == "Blocked by rule ask-before-delete: Ask a human before deleting."
    assert allowed.content == "ran"
    assert [a for a in tool.ran if "name" in a] == [{"name": "users"}]


def test_an_approval_in_another_thread_does_not_count(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    with thread("t1"):
        run(mw, tool, "ask_human", {})
    with thread("t2"):
        result = run(mw, tool, "drop_table", {"name": "users"})
    assert result.content == "Blocked by rule ask-before-delete: Ask a human before deleting."


def test_an_errored_approval_call_does_not_count(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        run(mw, Tool(status="error"), "ask_human", {})
        result = run(mw, tool, "drop_table", {"name": "users"})
    assert tool.ran == []
    assert result.content == "Blocked by rule ask-before-delete: Ask a human before deleting."


# Observe mode


def test_observe_mode_runs_the_call_logs_a_would_block_and_never_counts(tmp_path, repo):
    trace = tmp_path / "trace.jsonl"
    _, mw = make(tmp_path, OBSERVE, trace_path=trace)
    tool = Tool()
    with thread():
        results = [run(mw, tool, "run_shell", {"cmd": "ls"}, f"c{i}") for i in range(4)]
    assert [r.content for r in results] == ["ran"] * 4
    assert len(tool.ran) == 4  # no GuardLoopError on the 3rd or 4th
    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    assert lines[0]["type"] == "gate_block"
    assert lines[0]["reason"] == "would block (observe mode): Do not run shell commands."
    assert lines[0]["constraint_id"] == "no-shell"


# Re-pin


def test_a_task_delegation_ends_with_the_rule_text_and_the_agent_runs_it(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    with thread(agent="planner"):
        run(mw, tool, "task", {"description": "Add a field.", "subagent_type": "coder"})
    assert tool.ran == [
        {
            "description": "Add a field.\n\nRules from the user that apply to this work:\n"
            "- Never touch the migrations folder.\n"
            "- Do not run shell commands.\n"
            "- Ask a human before deleting.",
            "subagent_type": "coder",
        }
    ]


@pytest.mark.parametrize("sub", ["coder", "reviewer", "general-purpose", "x_1"])
def test_the_all_scope_reaches_every_subagent_name(sub):
    constraint = Constraint.model_validate(
        {
            "id": "no-shell",
            "text": "No shell.",
            "kind": "constraint",
            "source": "user",
            "authority": "binding",
            "scope": ["all"],
            "origin": "declared",
            "check": {"type": "tool_forbidden", "name": "run_shell"},
        }
    )
    assert repin("Do it.", sub, [constraint]) == (
        "Do it.\n\nRules from the user that apply to this work:\n- No shell."
    )


def test_the_gated_arm_re_pins_a_rule_whose_scope_is_all(tmp_path):
    # Breaks if `sub in c.scope` stays the only test: "all" is not an agent name.
    constraint = Constraint.model_validate(
        {
            "id": "no-shell",
            "text": "No shell.",
            "kind": "constraint",
            "source": "user",
            "authority": "binding",
            "scope": ["all"],
            "origin": "declared",
            "check": {"type": "tool_forbidden", "name": "run_shell"},
        }
    )
    run_config = RunConfig.model_validate(
        {
            "run_id": "r",
            "task_id": "X1",
            "arm": "gated",
            "operator": "none",
            "target_boundary": "delegation:lead->worker",
            "target_constraint": "no-shell",
            "ledger_path": str(tmp_path / "l.yaml"),
            "trace_path": str(tmp_path / "t.jsonl"),
            "seed": 1,
        }
    )
    writer = TraceWriter(run_config, model_id="m", safe_config_hash="s", ledger_hash="l")
    gate_mw = GateMiddleware(
        run_config, Ledger(version=1, constraints=(constraint,)), writer, None, gated=True
    )
    assert gate_mw._repinned("worker", "Do it.").endswith("- No shell.")


def test_a_blocked_task_call_is_not_re_pinned_and_does_not_run(tmp_path, repo):
    rules = RULES.replace("name: run_shell", "name: task")
    _, mw = make(tmp_path, rules)
    tool = Tool()
    with thread():
        result = run(mw, tool, "task", {"description": "x", "subagent_type": "coder"})
    assert tool.ran == []
    assert result.content == "Blocked by rule no-shell: Do not run shell commands."


# Async


def test_the_async_path_blocks_counts_and_runs_like_the_sync_one(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    blocked = {"file_path": "migrations/001.sql"}

    async def scenario():
        with thread():
            first = await mw.awrap_tool_call(req("write_file", blocked), tool.acall)
            second = await mw.awrap_tool_call(req("write_file", blocked), tool.acall)
            with pytest.raises(GuardLoopError, match="no-migrations"):
                await mw.awrap_tool_call(req("write_file", blocked), tool.acall)
            ok = await mw.awrap_tool_call(req("write_file", {"file_path": "app/a.py"}), tool.acall)
            await mw.awrap_tool_call(req("ask_human", {}), tool.acall)
            drop = await mw.awrap_tool_call(req("drop_table", {"name": "u"}), tool.acall)
        return first, second, ok, drop

    first, second, ok, drop = asyncio.run(scenario())
    assert (first.content, second.content) == (BLOCK_MIGRATIONS, BLOCK_MIGRATIONS)
    assert (ok.content, drop.content) == ("ran", "ran")
    assert tool.ran == [{"file_path": "app/a.py"}, {}, {"name": "u"}]


# No model calls, trace


def test_the_guard_makes_no_model_calls(tmp_path, repo, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the guard asked for a model")

    monkeypatch.setattr(nemogate.common.llm, "get_model", boom)
    monkeypatch.setattr(gate, "get_model", boom)
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        run(mw, tool, "write_file", {"file_path": "migrations/001.sql"})
        run(mw, tool, "write_file", {"file_path": "app/a.py"})
        run(mw, tool, "task", {"description": "x", "subagent_type": "coder"})
        run(mw, tool, "ask_human", {})
    assert len(tool.ran) == 3


def test_there_is_no_trace_file_unless_trace_path_is_given(tmp_path, repo):
    _, mw = make(tmp_path)
    with thread():
        run(mw, Tool(), "write_file", {"file_path": "migrations/001.sql"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["repo", "rules.yaml"]


def test_each_decision_writes_one_stamped_trace_line(tmp_path, repo):
    trace = tmp_path / "logs" / "trace.jsonl"
    guard, mw = make(tmp_path, trace_path=trace)
    with thread():
        run(mw, Tool(), "write_file", {"file_path": "migrations/001.sql"})
        run(mw, Tool(), "write_file", {"file_path": "app/a.py"}, "c2")
    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [(e["step"], e["type"], e["agent"]) for e in lines] == [
        (0, "gate_block", "coder"),
        (1, "tool_call", "coder"),
    ]
    assert lines[0]["constraint_id"] == "no-migrations"
    assert lines[0]["tool"] == "write_file"
    assert lines[0]["reason"] == BLOCK_MIGRATIONS
    assert lines[1]["args"] == {"file_path": "app/a.py"}
    assert lines[1]["blocked"] is False
    stamp = {(e["model_id"], e["safe_config_hash"], e["ledger_hash"]) for e in lines}
    assert len(stamp) == 1
    model_id, safe_hash, ledger_hash = stamp.pop()
    assert (model_id, safe_hash) == ("none", "none")
    assert len(ledger_hash) == 12


def test_the_guard_and_its_subagent_instances_share_one_trace_and_one_step_counter(tmp_path, repo):
    trace = tmp_path / "trace.jsonl"
    guard = Guard.from_yaml(_write(tmp_path, RULES), trace_path=trace)
    (main,), (sub,) = guard.middleware(), guard.middleware()
    with thread(agent="planner"):
        run(main, Tool(), "write_file", {"file_path": "app/a.py"})
    with thread(agent="coder"):
        run(sub, Tool(), "write_file", {"file_path": "app/b.py"}, "c2")
    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [(e["step"], e["agent"]) for e in lines] == [(0, "planner"), (1, "coder")]


def test_an_agent_name_the_trace_cannot_hold_is_still_traced(tmp_path, repo):
    trace = tmp_path / "trace.jsonl"
    _, mw = make(tmp_path, trace_path=trace)
    with thread(agent="code reviewer"):
        run(mw, Tool(), "write_file", {"file_path": "app/a.py"})
    assert json.loads(trace.read_text())["agent"] == "code_reviewer"


# Deep Agents' virtual paths: "/migrations/x.sql" is the repo's migrations/x.sql


@pytest.mark.parametrize(
    "path",
    ["/migrations/x.sql", "/app/../migrations/x.sql", "../migrations/x.sql", "//migrations/x.sql"],
)
def test_a_virtual_absolute_path_into_the_forbidden_folder_is_blocked(tmp_path, repo, path):
    # Breaks if only the host-resolved path is matched: /migrations/x.sql is outside the cwd
    # there, but it is the protected file to Deep Agents' file tools.
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        result = run(mw, tool, "write_file", {"file_path": path})
    assert tool.ran == []
    assert result.content == BLOCK_MIGRATIONS


def test_a_virtual_absolute_path_to_an_allowed_file_runs(tmp_path, repo):
    _, mw = make(tmp_path)
    tool = Tool()
    with thread():
        run(mw, tool, "write_file", {"file_path": "/app/main.py"})
        run(mw, tool, "write_file", {"file_path": "/other/migrations/x.sql"}, "c2")
    assert len(tool.ran) == 2


# Deleting a parent of the protected folder


NESTED = """\
version: 1
tools:
  rm_path: {path_arg: path, op: delete}
rules:
  - id: keep-migrations
    text: Keep app/migrations.
    check:
      path_forbidden: {glob: "app/migrations/**", ops: [delete]}
"""
KEEP = "Blocked by rule keep-migrations: Keep app/migrations."


@pytest.mark.parametrize("path", ["app", "/app", "app/", "./app", "/", "app/migrations"])
def test_deleting_a_parent_of_the_protected_folder_is_blocked(tmp_path, repo, path):
    # Breaks if only the target itself is matched: deleting app removes app/migrations/001.sql.
    _, mw = make(tmp_path, NESTED)
    tool = Tool()
    with thread():
        result = run(mw, tool, "delete", {"file_path": path})
    assert tool.ran == []
    assert result.content == KEEP


def test_deleting_a_custom_tool_parent_is_blocked_and_a_sibling_file_is_not(tmp_path, repo):
    _, mw = make(tmp_path, NESTED)
    tool = Tool()
    with thread():
        blocked = run(mw, tool, "rm_path", {"path": "app"})
        run(mw, tool, "delete", {"file_path": "app/main.py"}, "c2")
        run(mw, tool, "delete", {"file_path": "/app/main.py"}, "c3")
        run(mw, tool, "delete", {"file_path": "other"}, "c4")
    assert blocked.content == KEEP
    assert tool.ran == [
        {"file_path": "app/main.py"},
        {"file_path": "/app/main.py"},
        {"file_path": "other"},
    ]


def test_writing_a_parent_of_the_protected_folder_is_not_a_delete(tmp_path, repo):
    _, mw = make(tmp_path, NESTED)
    tool = Tool()
    with thread():
        run(mw, tool, "write_file", {"file_path": "app"})
    assert len(tool.ran) == 1


# An approval tool that answers no


def approval_run(tmp_path, answer):
    _, mw = make(tmp_path)
    drop = Tool()
    with thread():
        mw.wrap_tool_call(req("ask_human", {}), lambda r: answer(r))
        result = run(mw, drop, "drop_table", {"name": "users"})
    return result, drop


def message(content, status="success"):
    return lambda r: ToolMessage(
        content=content, tool_call_id="c1", name="ask_human", status=status
    )


@pytest.mark.parametrize(
    "answer",
    [
        message('{"approved": false, "reason": "Human refused"}'),
        message('{"approve": "false"}'),
        message('{"approved": 0}'),
        message('{"approved": "False"}'),
        lambda r: {"approved": False, "reason": "Human refused"},
    ],
)
def test_an_approval_tool_that_returns_a_denial_does_not_approve(tmp_path, repo, answer):
    # Breaks if "returned without error" is enough: the tool ran fine and said no.
    result, drop = approval_run(tmp_path, answer)
    assert drop.ran == []
    assert result.content == "Blocked by rule ask-before-delete: Ask a human before deleting."


@pytest.mark.parametrize(
    "answer",
    [
        message('{"approved": true}'),
        message("Approved by alice."),
        message("not json {approved: false"),
        message('["approved", false]'),
        message('{"reason": "no key"}'),
        lambda r: {"approved": True},
    ],
)
def test_any_other_approval_result_without_error_still_approves(tmp_path, repo, answer):
    result, drop = approval_run(tmp_path, answer)
    assert drop.ran == [{"name": "users"}]
    assert result.content == "ran"


# A trace that cannot be written never changes a decision


class FullDisk:
    def __call__(self, *args, **kwargs):
        raise OSError(28, "No space left on device")


def test_a_failing_trace_write_changes_no_decision_and_warns_once(
    tmp_path, repo, monkeypatch, caplog
):
    # Breaks if the trace write is on the decision's path: ENOSPC would abort the call.
    guard, mw = make(tmp_path, OBSERVE, trace_path=tmp_path / "trace.jsonl")
    monkeypatch.setattr(guard._writer, "emit", FullDisk())
    tool = Tool()
    with caplog.at_level("WARNING", logger="nemogate.gate.middleware"), thread():
        blocked = run(mw, tool, "write_file", {"file_path": "migrations/001.sql"})
        allowed = run(mw, tool, "write_file", {"file_path": "app/a.py"}, "c2")
        observed = run(mw, tool, "run_shell", {"cmd": "ls"}, "c3")
        approved = run(mw, tool, "ask_human", {}, "c4")
        run(mw, tool, "write_file", {"file_path": "migrations/001.sql"})
        with pytest.raises(GuardLoopError):
            run(mw, tool, "write_file", {"file_path": "migrations/001.sql"})
    assert blocked.content == BLOCK_MIGRATIONS
    assert (allowed.content, observed.content, approved.content) == ("ran", "ran", "ran")
    assert tool.ran == [{"file_path": "app/a.py"}, {"cmd": "ls"}, {}]
    assert [r.levelname for r in caplog.records if "trace" in r.message] == ["WARNING"]


# A real Deep Agents graph (scripted model), where file paths are virtual


class ScriptedChat(BaseChatModel):
    """Calls the given tools one after the other, then answers in text."""

    calls: list

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < len(self.calls):
            name, args = self.calls[done]
            reply = AIMessage(
                content="", tool_calls=[{"name": name, "args": args, "id": f"call-{done}"}]
            )
        else:
            reply = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=reply)])


def test_a_real_graph_blocks_a_virtual_absolute_write_into_the_forbidden_folder(tmp_path):
    # Breaks if paths are only read as host paths: in the graph "/migrations/x.sql" is created
    # in the agent's files although the rule says migrations/**.
    guard = Guard.from_yaml(_write(tmp_path, RULES))
    model = ScriptedChat(
        calls=[
            ("write_file", {"file_path": "/migrations/x.sql", "content": "drop"}),
            ("write_file", {"file_path": "/app/ok.py", "content": "fine"}),
        ]
    )
    agent = create_deep_agent(model=model, middleware=guard.middleware())
    out = agent.invoke(
        {"messages": [("user", "go")]}, {"configurable": {"thread_id": "real-graph"}}
    )
    replies = [m.content for m in out["messages"] if isinstance(m, ToolMessage)]
    assert replies[0] == BLOCK_MIGRATIONS
    assert sorted(out["files"]) == ["/app/ok.py"]


# Which deletes can remove something a glob protects


@pytest.mark.parametrize(
    ("glob", "target", "expected"),
    [
        ("app/migrations/**", "app", True),
        ("app/migrations/**", ".", True),
        ("app/migrations/**", "app/migrations", True),
        ("app/migrations/**", "app/main.py", False),
        ("app/migrations/**", "other", False),
        ("app/*/migrations/**", "app", True),
        ("app/*/migrations/**", "app/team", True),
        ("app/*/migrations/**", "app/team/migrations", True),
        ("app/*/migrations/**", "other/team", False),
        # fnmatch lets * cross "/" (oracle/paths.py), so app/a/b/migrations/x is protected too
        ("app/*/migrations/**", "app/a/b", True),
        ("**/migrations/**", "app", True),
        ("**/migrations/**", "app/main.py", True),  # a leading ** blocks every delete: exact
        ("data/v?/old", "data/v1", True),
        ("data/v?/old", "data/w1", False),
        ("data/v?/old", "data/v1/old/x", False),  # below the match: the oracle's own check
        # a * segment is matched as a whole: "src/pkg/private/key" matches src*/private/**
        ("src*/private/**", "src/pkg", True),
        ("src*/private/**", "src", True),
        ("src*/private/**", "srcx/pkg/deep", True),
        ("src*/private/**", "other/pkg", False),
        ("src*/private/**", "other", False),
    ],
)
def test_glob_may_match_below_says_whether_a_delete_of_target_can_remove_a_match(
    glob, target, expected
):
    assert glob_may_match_below(glob, target) is expected


@pytest.mark.parametrize(
    ("glob", "path", "blocked"),
    [
        ("app/*/migrations/**", "/app/team", True),
        ("app/*/migrations/**", "app/team/", True),
        ("**/migrations/**", "/app", True),
        ("**/migrations/**", "app", True),
        ("app/migrations/**", "app/main.py", False),
    ],
)
def test_a_delete_is_blocked_when_a_wildcard_glob_can_match_below_it(
    tmp_path, repo, glob, path, blocked
):
    # Breaks if only the glob's leading literal segments are compared: delete("app/team")
    # removes app/team/migrations/001.sql under app/*/migrations/**.
    rules = f"""\
version: 1
rules:
  - id: keep
    text: Keep it.
    check:
      path_forbidden: {{glob: "{glob}", ops: [delete]}}
"""
    _, mw = make(tmp_path, rules)
    tool = Tool()
    with thread():
        result = run(mw, tool, "delete", {"file_path": path})
    assert (tool.ran == []) is blocked
    assert (result.content == "Blocked by rule keep: Keep it.") is blocked


def test_a_delete_of_a_parent_the_star_crosses_into_is_blocked_and_an_unrelated_one_runs(
    tmp_path, repo
):
    # Breaks if every directory a * segment crosses must repeat the segment's literal prefix:
    # delete("src/pkg") removed src/pkg/private/key, which src*/private/** protects.
    rules = """\
version: 1
rules:
  - id: keep
    text: Keep it.
    check:
      path_forbidden: {glob: "src*/private/**", ops: [delete]}
"""
    _, mw = make(tmp_path, rules)
    tool = Tool()
    with thread():
        blocked = run(mw, tool, "delete", {"file_path": "src/pkg"})
        run(mw, tool, "delete", {"file_path": "other/pkg"}, "c2")
    assert blocked.content == "Blocked by rule keep: Keep it."
    assert tool.ran == [{"file_path": "other/pkg"}]


# Deleting an absolute ancestor of the working directory


def test_an_absolute_ancestor_of_the_root_is_blocked_like_its_dotdot_form(tmp_path, monkeypatch):
    # Breaks if an absolute path outside the root is allowed without asking what it contains:
    # delete("/tmp/...") removed the root and its private/ folder, while delete("..") was blocked.
    outer = tmp_path.resolve()
    root = outer / "x"
    root.mkdir()
    monkeypatch.chdir(root)
    rules = """\
version: 1
rules:
  - id: keep
    text: Keep it.
    check:
      path_forbidden: {glob: "private/**", ops: [delete]}
"""
    _, mw = make(tmp_path, rules)
    tool = Tool()
    kept = "Blocked by rule keep: Keep it."
    with thread():
        results = {
            "outer": run(mw, tool, "delete", {"file_path": str(outer)}, "c1"),
            "slash": run(mw, tool, "delete", {"file_path": "/"}, "c2"),
            "dotdot": run(mw, tool, "delete", {"file_path": ".."}, "c3"),
            "root": run(mw, tool, "delete", {"file_path": str(root)}, "c4"),
            "private": run(mw, tool, "delete", {"file_path": str(root / "private")}, "c5"),
            "sibling": run(mw, tool, "delete", {"file_path": str(outer / "y")}, "c6"),
        }
    assert {k: v.content == kept for k, v in results.items()} == {
        "outer": True,
        "slash": True,
        "dotdot": True,
        "root": True,
        "private": True,
        "sibling": False,
    }
    assert tool.ran == [{"file_path": str(outer / "y")}]


# Approval answers that mean no


@pytest.mark.parametrize(
    "content",
    [
        '{"approved": 0.0}',
        '{"approved": -0.0}',
        '{"approved": 0e0}',
        '{"approved": 0}',
        '{"approve": false}',
        '{"approved": "no"}',
        '{"approved": " No "}',
        '{"approved": "denied"}',
        '{"approved": "0.0"}',
        '{"approved": "0"}',
    ],
)
def test_every_spelling_of_no_in_an_approval_answer_denies(tmp_path, repo, content):
    # Breaks if only False, "false" and 0 are read as no: a tool answering 0.0 or "no" ran fine.
    result, drop = approval_run(tmp_path, message(content))
    assert drop.ran == []
    assert result.content == "Blocked by rule ask-before-delete: Ask a human before deleting."


@pytest.mark.parametrize(
    "answer",
    [
        message('{"approved": true}'),
        message('{"approved": 1}'),
        message('{"approved": "yes"}'),
        message('{"approved": 0.5}'),
        message('{"approved": "nan"}'),
        message('{"approved": null}'),
        lambda r: {"approved": 1.0},
    ],
)
def test_a_yes_in_an_approval_answer_still_approves(tmp_path, repo, answer):
    result, drop = approval_run(tmp_path, answer)
    assert drop.ran == [{"name": "users"}]


def test_a_deeply_nested_approval_answer_is_not_a_denial_and_does_not_crash(tmp_path, repo):
    # Breaks if json.loads' RecursionError escapes the hook and aborts the graph.
    nested = "[" * 10000 + "0" + "]" * 10000
    result, drop = approval_run(tmp_path, message(nested))
    assert drop.ran == [{"name": "users"}]
