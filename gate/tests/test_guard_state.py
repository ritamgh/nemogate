"""Per-thread hold-back counters, approvals, the default thread and the 3-strike loop stop,
tried against the real GuardMiddleware (direct calls with real ToolCallRequest objects) and real
Deep Agents graphs with scripted chat models (FR-017, FR-018, FR-019, E-4, E-5, E-6, DEC-8, DEC-10).
"""

import asyncio
import sys
import threading
from collections import Counter

import pytest
from deepagents import create_deep_agent
from deepagents.middleware.subagents import SubAgent
from langchain_core.messages import ToolMessage
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tools import tool

from gate.errors import GuardLoopError
from gate.guard import Guard
from gate.tests.test_guard_wiring import ScriptedChat, Tool, req, thread

RULES = """\
version: 1
approval_tool: ask_human
rules:
  - id: no-shell
    text: Do not run shell commands.
    check:
      tool_forbidden: {name: run_shell}
  - id: no-python
    text: Do not run python.
    check:
      tool_forbidden: {name: run_python}
  - id: no-migrations
    text: Never touch the migrations folder.
    check:
      path_forbidden: {glob: "migrations/**", ops: [write, delete]}
  - id: ask-before-drop
    text: Ask a human before dropping a table.
    check:
      approval_before: {tool: drop_table}
"""
SHELL = "Blocked by rule no-shell: Do not run shell commands."
PYTHON = "Blocked by rule no-python: Do not run python."
MIGRATIONS = "Blocked by rule no-migrations: Never touch the migrations folder."
DROP = "Blocked by rule ask-before-drop: Ask a human before dropping a table."

# One observe rule and one block rule on the same tool.
OBSERVE_AND_BLOCK = """\
version: 1
rules:
  - id: watch-shell
    text: Shell use is watched.
    mode: observe
    check:
      tool_forbidden: {name: run_shell}
  - id: no-shell
    text: Do not run shell commands.
    check:
      tool_forbidden: {name: run_shell}
"""
OBSERVE_ONLY = OBSERVE_AND_BLOCK.split("  - id: no-shell")[0]
# Two block rules that break on the same call.
TWO_RULES_ONE_CALL = """\
version: 1
rules:
  - id: no-shell
    text: Do not run shell commands.
    check:
      tool_forbidden: {name: run_shell}
  - id: no-anything
    text: Do not run anything.
    check:
      tool_forbidden: {name: run_shell}
"""


@pytest.fixture(autouse=True)
def _in_empty_repo(tmp_path, monkeypatch):
    """Relative paths resolve against the cwd; keep it an empty folder."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)


def make(tmp_path, text=RULES):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    guard = Guard.from_yaml(path)
    (mw,) = guard.middleware()
    return guard, mw


def run(mw, tool_, name, args, call_id="c1"):
    return mw.wrap_tool_call(req(name, args, call_id), tool_)


def attempt(mw, tool_, name, args, call_id="c1"):
    """The reply text of a call, or the GuardLoopError it raised."""
    try:
        return run(mw, tool_, name, args, call_id).content
    except GuardLoopError as exc:
        return exc


# 1. The counter key: (thread, rule, tool, canonical arguments)


def test_the_same_blocked_call_twice_returns_the_rule_text_and_the_third_raises(tmp_path):
    # Breaks if the stop moves to attempt 2 or 4, or the tool runs on any attempt.
    _, mw = make(tmp_path)
    ran = Tool()
    with thread("t1"):
        first = run(mw, ran, "run_shell", {"cmd": "ls"}, "c1")
        second = run(mw, ran, "run_shell", {"cmd": "ls"}, "c2")
        with pytest.raises(GuardLoopError) as raised:
            run(mw, ran, "run_shell", {"cmd": "ls"}, "c3")
    assert [(m.content, m.status, m.tool_call_id) for m in (first, second)] == [
        (SHELL, "error", "c1"),
        (SHELL, "error", "c2"),
    ]
    assert (raised.value.rule_id, raised.value.tool, raised.value.thread_id) == (
        "no-shell",
        "run_shell",
        "t1",
    )
    assert ran.ran == []


def test_a_caller_that_swallows_the_error_and_retries_is_stopped_again(tmp_path):
    # Breaks if the count is reset after the raise: an app that catches GuardLoopError and tries
    # a fourth time must not get a fourth plain block.
    _, mw = make(tmp_path)
    ran = Tool()
    with thread("t1"):
        outcomes = [attempt(mw, ran, "run_shell", {"cmd": "ls"}) for _ in range(5)]
    assert outcomes[:2] == [SHELL, SHELL]
    assert [isinstance(o, GuardLoopError) for o in outcomes] == [False, False, True, True, True]
    assert ran.ran == []


def test_allowed_calls_between_blocks_do_not_reset_the_count(tmp_path):
    # Breaks if a call that runs clears the counters: block, allowed, block, allowed, block.
    _, mw = make(tmp_path)
    ran = Tool()
    with thread("t1"):
        outcomes = []
        for i in range(3):
            outcomes.append(attempt(mw, ran, "run_shell", {"cmd": "ls"}, f"b{i}"))
            run(mw, ran, "read_file", {"file_path": f"app/{i}.py"}, f"ok{i}")
    assert outcomes[:2] == [SHELL, SHELL]
    assert isinstance(outcomes[2], GuardLoopError)
    assert ran.ran == [
        {"file_path": "app/0.py"},
        {"file_path": "app/1.py"},
        {"file_path": "app/2.py"},
    ]


# 2. Argument identity


def test_reordered_argument_keys_count_as_the_same_call(tmp_path):
    # Breaks if canonical_args drops sort_keys: a model that shuffles keys would dodge the stop.
    _, mw = make(tmp_path)
    ran = Tool()
    with thread("t1"):
        first = attempt(mw, ran, "run_shell", {"cmd": "ls", "opts": {"a": 1, "b": 2}})
        second = attempt(mw, ran, "run_shell", {"opts": {"b": 2, "a": 1}, "cmd": "ls"})
        third = attempt(mw, ran, "run_shell", {"opts": {"a": 1, "b": 2}, "cmd": "ls"})
    assert (first, second) == (SHELL, SHELL)
    assert isinstance(third, GuardLoopError)
    assert ran.ran == []


@pytest.mark.parametrize(
    "changed",
    [
        {"cmd": "ls -l", "flags": ["-a", "-l"], "n": 1},
        {"cmd": "ls", "flags": ["-l", "-a"], "n": 1},  # list order is part of the call
        {"cmd": "ls", "flags": ["-a", "-l"], "n": 1, "extra": None},
        {"cmd": "ls", "flags": ["-a", "-l"], "n": "1"},  # 1 and "1" are different arguments
        {"cmd": "ls", "flags": ["-a", "-l"]},
    ],
)
def test_a_changed_argument_starts_a_new_count_at_one(tmp_path, changed):
    # Breaks if the counter ignores the arguments (keyed by rule and tool only): the changed call
    # would inherit count 2 and the next one would raise early.
    _, mw = make(tmp_path)
    ran = Tool()
    base = {"cmd": "ls", "flags": ["-a", "-l"], "n": 1}
    with thread("t1"):
        attempt(mw, ran, "run_shell", base, "a1")
        attempt(mw, ran, "run_shell", base, "a2")
        first_of_changed = attempt(mw, ran, "run_shell", changed, "b1")
        second_of_changed = attempt(mw, ran, "run_shell", changed, "b2")
        base_again = attempt(mw, ran, "run_shell", base, "a3")
        changed_again = attempt(mw, ran, "run_shell", changed, "b3")
    assert (first_of_changed, second_of_changed) == (SHELL, SHELL)
    assert isinstance(base_again, GuardLoopError)
    assert isinstance(changed_again, GuardLoopError)
    assert ran.ran == []


def test_the_same_arguments_on_another_tool_are_counted_apart(tmp_path):
    # Breaks if the key leaves out the tool name.
    _, mw = make(tmp_path)
    ran = Tool()
    with thread("t1"):
        attempt(mw, ran, "run_shell", {"cmd": "ls"})
        attempt(mw, ran, "run_shell", {"cmd": "ls"})
        other = attempt(mw, ran, "run_python", {"cmd": "ls"})
    assert other == PYTHON


def test_a_call_that_breaks_two_rules_counts_against_each_and_the_first_rule_is_named(tmp_path):
    # Breaks if only one rule gets the count, or the error names the wrong rule (E-2, FR-003).
    _, mw = make(tmp_path, TWO_RULES_ONE_CALL)
    ran = Tool()
    both = (
        "Blocked by rule no-shell: Do not run shell commands.\n"
        "Blocked by rule no-anything: Do not run anything."
    )
    with thread("t1"):
        first = run(mw, ran, "run_shell", {"cmd": "ls"}).content
        second = run(mw, ran, "run_shell", {"cmd": "ls"}).content
        with pytest.raises(GuardLoopError) as raised:
            run(mw, ran, "run_shell", {"cmd": "ls"})
    assert (first, second) == (both, both)
    assert raised.value.rule_id == "no-shell"
    assert ran.ran == []


def test_the_path_rule_counts_the_call_as_written_not_as_resolved(tmp_path):
    # Breaks if the count key uses the resolved path: two different spellings of one forbidden
    # file are two different calls, each at count 1.
    _, mw = make(tmp_path)
    ran = Tool()
    with thread("t1"):
        results = [
            attempt(mw, ran, "write_file", {"file_path": "migrations/001.sql"}, "c1"),
            attempt(mw, ran, "write_file", {"file_path": "migrations/001.sql"}, "c2"),
            attempt(mw, ran, "write_file", {"file_path": "app/../migrations/001.sql"}, "c3"),
            attempt(mw, ran, "write_file", {"file_path": "app/../migrations/001.sql"}, "c4"),
        ]
    assert results == [MIGRATIONS] * 4
    assert ran.ran == []


# 3. Threads


def test_thread_x_blocked_twice_leaves_thread_y_at_zero(tmp_path):
    # Breaks if the thread is left out of the key: Y's first call would already be count 3.
    _, mw = make(tmp_path)
    ran = Tool()
    with thread("x"):
        attempt(mw, ran, "run_shell", {"cmd": "ls"})
        attempt(mw, ran, "run_shell", {"cmd": "ls"})
    with thread("y"):
        y_first = attempt(mw, ran, "run_shell", {"cmd": "ls"})
        y_second = attempt(mw, ran, "run_shell", {"cmd": "ls"})
        y_third = attempt(mw, ran, "run_shell", {"cmd": "ls"})
    with thread("x"):
        x_third = attempt(mw, ran, "run_shell", {"cmd": "ls"})
    assert (y_first, y_second) == (SHELL, SHELL)
    assert isinstance(y_third, GuardLoopError) and y_third.thread_id == "y"
    assert isinstance(x_third, GuardLoopError) and x_third.thread_id == "x"
    assert ran.ran == []


def test_calls_of_two_threads_interleaved_are_counted_apart(tmp_path):
    # Breaks if "the last thread seen" is kept as state instead of reading it per call.
    _, mw = make(tmp_path)
    ran = Tool()
    outcomes = []
    for tid in ["a", "b", "a", "b", "a", "b"]:
        with thread(tid):
            outcomes.append((tid, attempt(mw, ran, "run_shell", {"cmd": "ls"})))
    assert [(tid, o if isinstance(o, str) else "raised") for tid, o in outcomes] == [
        ("a", SHELL),
        ("b", SHELL),
        ("a", SHELL),
        ("b", SHELL),
        ("a", "raised"),
        ("b", "raised"),
    ]


def test_thread_id_zero_is_a_thread_of_its_own_not_the_default(tmp_path):
    # Breaks if a falsy thread id (0) is replaced by the default thread.
    _, mw = make(tmp_path)
    ran = Tool()
    with thread(0):
        attempt(mw, ran, "run_shell", {"cmd": "ls"})
        attempt(mw, ran, "run_shell", {"cmd": "ls"})
    with thread(None):
        in_default = attempt(mw, ran, "run_shell", {"cmd": "ls"})
    with thread(0):
        in_zero = attempt(mw, ran, "run_shell", {"cmd": "ls"})
    assert in_default == SHELL
    assert isinstance(in_zero, GuardLoopError) and in_zero.thread_id == "0"


# 4. Subagents under one thread share one counter


def shell_tool(ran):
    @tool
    def run_shell(cmd: str) -> str:
        """Run a shell command."""
        ran.append(cmd)
        return "ran"

    return run_shell


SHELL_CALL = ("run_shell", {"cmd": "ls"})


def delegate(name):
    return ("task", {"description": "Do the work.", "subagent_type": name})


def build(guard, ran, main_calls, subagents):
    """A real Deep Agents graph: the guard in the main list and in every subagent spec."""
    shell = shell_tool(ran)
    specs = [
        SubAgent(
            name=name,
            description=name,
            system_prompt=name,
            model=ScriptedChat(calls=calls),
            tools=[shell],
            middleware=guard.middleware(),
        )
        for name, calls in subagents.items()
    ]
    return create_deep_agent(
        model=ScriptedChat(calls=main_calls),
        tools=[shell],
        middleware=guard.middleware(),
        subagents=specs,
    )


def tool_replies(out):
    return [m.content for m in out["messages"] if isinstance(m, ToolMessage)]


def guard_for(tmp_path, text=RULES):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    return Guard.from_yaml(path)


def test_two_subagents_and_their_parent_share_one_counter_in_a_real_graph(tmp_path):
    # Breaks if a subagent's guard instance keeps its own state, or its thread differs from the
    # parent's (docs/FACTS.md section 13): coder 1 + reviewer 1 + main 1 is the third strike.
    ran = []
    agent = build(
        guard_for(tmp_path),
        ran,
        [delegate("coder"), delegate("reviewer"), SHELL_CALL],
        {"coder": [SHELL_CALL], "reviewer": [SHELL_CALL]},
    )
    with pytest.raises(GuardLoopError) as raised:
        agent.invoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "T"}})
    assert (raised.value.rule_id, raised.value.thread_id) == ("no-shell", "T")
    assert ran == []


def test_the_third_strike_inside_a_subagent_stops_the_whole_run(tmp_path):
    # Breaks if the task tool or the graph swallows the error: the run would go on with the
    # subagent's reply instead of stopping.
    ran = []
    agent = build(
        guard_for(tmp_path),
        ran,
        [delegate("coder"), delegate("reviewer")],
        {"coder": [SHELL_CALL, SHELL_CALL], "reviewer": [SHELL_CALL]},
    )
    with pytest.raises(GuardLoopError) as raised:
        agent.invoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "T"}})
    assert raised.value.thread_id == "T"
    assert ran == []


def test_two_blocks_in_one_subagent_and_the_same_call_in_another_thread_are_apart(tmp_path):
    # Breaks if subagents of different threads share counts: thread U's subagent is at count 1.
    ran = []
    agent = build(
        guard_for(tmp_path),
        ran,
        [delegate("coder")],
        {"coder": [SHELL_CALL, SHELL_CALL]},
    )
    first = agent.invoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "T"}})
    second = agent.invoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "U"}})
    assert tool_replies(first) == ["done"] and tool_replies(second) == ["done"]
    third = None
    try:
        agent.invoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "T"}})
    except GuardLoopError as exc:
        third = exc
    assert third is not None and third.thread_id == "T"
    assert ran == []


def test_two_runs_on_one_thread_continue_the_count_in_a_real_graph(tmp_path):
    # Breaks if the counters reset per invoke: FR-017 says two runs on one thread share them.
    ran = []
    agent = build(guard_for(tmp_path), ran, [SHELL_CALL], {})
    config = {"configurable": {"thread_id": "T"}}
    replies = [tool_replies(agent.invoke({"messages": [("user", "go")]}, config)) for _ in range(2)]
    assert replies == [[SHELL], [SHELL]]
    with pytest.raises(GuardLoopError):
        agent.invoke({"messages": [("user", "go")]}, config)
    assert ran == []


def test_async_subagents_share_the_parent_thread_counter(tmp_path):
    # Breaks on the async path only (AS-5 left ainvoke untested): the same three strikes over
    # ainvoke with a subagent in the middle.
    ran = []
    agent = build(
        guard_for(tmp_path),
        ran,
        [delegate("coder"), SHELL_CALL, SHELL_CALL],
        {"coder": [SHELL_CALL]},
    )

    async def scenario():
        await agent.ainvoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "A"}})

    with pytest.raises(GuardLoopError) as raised:
        asyncio.run(scenario())
    assert raised.value.thread_id == "A"
    assert ran == []


# 5. Approvals are keyed (thread, rule)


def approve(mw, thread_id, agent="coder", **answer):
    with thread(thread_id, agent=agent):
        return run(mw, Tool(**answer), "ask_human", {"question": "ok?"})


def test_an_approval_covers_its_own_thread_only(tmp_path):
    # Breaks if approvals are a plain set of rule ids: thread t2 would ride on t1's approval.
    _, mw = make(tmp_path)
    ran = Tool()
    approve(mw, "t1")
    with thread("t2"):
        in_t2 = run(mw, ran, "drop_table", {"name": "users"})
    with thread("t1"):
        in_t1 = run(mw, ran, "drop_table", {"name": "users"})
    with thread(None):
        in_default = run(mw, ran, "drop_table", {"name": "users"})
    assert in_t2.content == DROP and in_default.content == DROP
    assert in_t1.content == "ran"
    assert ran.ran == [{"name": "users"}]


def test_an_approval_call_that_returned_an_error_does_not_count(tmp_path):
    # Breaks if any call of the approval tool counts, whatever it returned.
    _, mw = make(tmp_path)
    ran = Tool()
    approve(mw, "t1", status="error")
    with thread("t1"):
        result = run(mw, ran, "drop_table", {"name": "users"})
    assert result.content == DROP
    assert ran.ran == []


def test_an_approval_call_that_raised_does_not_count(tmp_path):
    # Breaks if the approval is recorded before the tool answers.
    _, mw = make(tmp_path)
    ran = Tool()

    def refuses(request):
        raise RuntimeError("the human closed the window")

    with thread("t1"):
        with pytest.raises(RuntimeError, match="closed the window"):
            run(mw, refuses, "ask_human", {"question": "ok?"})
        result = run(mw, ran, "drop_table", {"name": "users"})
    assert result.content == DROP
    assert ran.ran == []


@pytest.mark.parametrize(
    "answer",
    [
        lambda r: {"approved": False},
        lambda r: ToolMessage(
            content='{"approved": false}', tool_call_id="c1", name="ask_human", status="success"
        ),
    ],
)
def test_an_approved_false_answer_does_not_count_in_any_thread(tmp_path, answer):
    # Breaks if "returned without error" is enough, here with two threads asking at once.
    _, mw = make(tmp_path)
    ran = Tool()
    for tid in ("t1", "t2"):
        with thread(tid):
            mw.wrap_tool_call(req("ask_human", {}), answer)
    for tid in ("t1", "t2"):
        with thread(tid):
            assert run(mw, ran, "drop_table", {"name": "users"}).content == DROP
    assert ran.ran == []


def test_a_denial_in_one_thread_leaves_another_threads_approval_alone(tmp_path):
    # Breaks if a denial clears approvals of every thread.
    _, mw = make(tmp_path)
    ran = Tool()
    approve(mw, "t1")
    with thread("t2"):
        mw.wrap_tool_call(req("ask_human", {}), lambda r: {"approved": False})
    with thread("t1"):
        assert run(mw, ran, "drop_table", {"name": "users"}).content == "ran"


def test_one_approval_covers_every_later_call_and_the_subagents_of_its_thread(tmp_path):
    # Breaks if the approval is used up by one call (FR-019: no timer, no use count), or does not
    # reach another agent of the same thread.
    _, mw = make(tmp_path)
    ran = Tool()
    approve(mw, "t1", agent="planner")
    with thread("t1", agent="coder"):
        results = [run(mw, ran, "drop_table", {"name": f"t{i}"}, f"c{i}") for i in range(3)]
    assert [r.content for r in results] == ["ran"] * 3
    assert len(ran.ran) == 3


def test_approval_after_two_blocks_lets_the_third_attempt_run_instead_of_raising(tmp_path):
    # Breaks if the strike count is checked even when the call is no longer blocked.
    _, mw = make(tmp_path)
    ran = Tool()
    args = {"name": "users"}
    with thread("t1"):
        blocked = [run(mw, ran, "drop_table", args, f"c{i}").content for i in range(2)]
        run(mw, ran, "ask_human", {})
        third = run(mw, ran, "drop_table", args, "c3")
    assert blocked == [DROP, DROP]
    assert third.content == "ran"
    assert ran.ran == [{}, {"name": "users"}]


def test_a_block_before_the_approval_is_counted_with_that_thread_only(tmp_path):
    # Breaks if approving thread t1 resets or shares counts: t2 blocked twice stays on its own.
    _, mw = make(tmp_path)
    ran = Tool()
    with thread("t2"):
        run(mw, ran, "drop_table", {"name": "users"})
        run(mw, ran, "drop_table", {"name": "users"})
    approve(mw, "t1")
    with thread("t2"), pytest.raises(GuardLoopError) as raised:
        run(mw, ran, "drop_table", {"name": "users"})
    assert (raised.value.rule_id, raised.value.thread_id) == ("ask-before-drop", "t2")


# 6. Observe mode never counts


def test_observe_mode_rules_never_count_however_often_they_fire(tmp_path):
    # Breaks if an observe hit is counted: the 3rd of 10 calls would raise.
    _, mw = make(tmp_path, OBSERVE_ONLY)
    ran = Tool()
    with thread("t1"):
        results = [run(mw, ran, "run_shell", {"cmd": "ls"}, f"c{i}") for i in range(10)]
    assert [r.content for r in results] == ["ran"] * 10
    assert len(ran.ran) == 10


def test_an_observe_rule_beside_a_block_rule_adds_no_count_and_no_text(tmp_path):
    # Breaks if observe hits join the block message or the strike count: the reply must carry the
    # block rule only, and the error must name it.
    _, mw = make(tmp_path, OBSERVE_AND_BLOCK)
    ran = Tool()
    with thread("t1"):
        first = run(mw, ran, "run_shell", {"cmd": "ls"}).content
        second = run(mw, ran, "run_shell", {"cmd": "ls"}).content
        with pytest.raises(GuardLoopError) as raised:
            run(mw, ran, "run_shell", {"cmd": "ls"})
    assert (first, second) == (SHELL, SHELL)
    assert raised.value.rule_id == "no-shell"
    assert ran.ran == []


# 7. A run with no thread_id: one shared default thread, one warning


def test_runs_without_a_thread_id_share_one_default_thread_and_warn_exactly_once(tmp_path, caplog):
    # Two runs with no thread id share counters ON PURPOSE (DEC-10, E-6): the guard cannot tell
    # them apart, so a dev who wants isolation must pass a thread_id. Breaks if the warning fires
    # per call or per run, or never.
    _, mw = make(tmp_path)
    ran = Tool()
    with caplog.at_level("WARNING"):
        with thread(None):  # run 1
            first = attempt(mw, ran, "run_shell", {"cmd": "ls"})
            run(mw, ran, "read_file", {"file_path": "app/a.py"})
        with thread(None):  # run 2, a different run, the same default thread
            second = attempt(mw, ran, "run_shell", {"cmd": "ls"})
            third = attempt(mw, ran, "run_shell", {"cmd": "ls"})
    assert (first, second) == (SHELL, SHELL)
    assert isinstance(third, GuardLoopError) and third.thread_id == "default"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "no thread_id in the run config" in warnings[0].getMessage()
    assert ran.ran == [{"file_path": "app/a.py"}]


@pytest.mark.parametrize(
    "config",
    [{"configurable": {"thread_id": None}}, {"configurable": {}}, {}, None],
    ids=["none-id", "no-id", "empty-config", "no-config"],
)
def test_every_way_of_having_no_thread_id_is_the_default_thread(tmp_path, config):
    # Breaks if only a missing key is handled: {"thread_id": None} must not become "None", and
    # a missing "configurable" must not crash.
    _, mw = make(tmp_path)
    ran = Tool()
    token = var_child_runnable_config.set(config)
    try:
        first = attempt(mw, ran, "run_shell", {"cmd": "ls"})
        second = attempt(mw, ran, "run_shell", {"cmd": "ls"})
        third = attempt(mw, ran, "run_shell", {"cmd": "ls"})
    finally:
        var_child_runnable_config.reset(token)
    assert (first, second) == (SHELL, SHELL)
    assert isinstance(third, GuardLoopError) and third.thread_id == "default"


def test_approvals_of_runs_without_a_thread_id_are_shared_as_well(tmp_path):
    # DEC-10 shares counters AND approvals under the default thread; this pins it down.
    _, mw = make(tmp_path)
    ran = Tool()
    with thread(None):
        run(mw, ran, "ask_human", {})
    with thread(None):
        result = run(mw, ran, "drop_table", {"name": "users"})
    assert result.content == "ran"


def test_the_default_thread_belongs_to_one_guard_object(tmp_path):
    # Breaks if the default thread's counts live at module level: a second guard starts clean
    # and warns on its own.
    _, mw_a = make(tmp_path)
    _, mw_b = make(tmp_path)
    ran = Tool()
    with thread(None):
        attempt(mw_a, ran, "run_shell", {"cmd": "ls"})
        attempt(mw_a, ran, "run_shell", {"cmd": "ls"})
        fresh = attempt(mw_b, ran, "run_shell", {"cmd": "ls"})
        stopped = attempt(mw_a, ran, "run_shell", {"cmd": "ls"})
    assert fresh == SHELL
    assert isinstance(stopped, GuardLoopError)


def test_a_second_guard_warns_about_the_missing_thread_id_on_its_own(tmp_path, caplog):
    _, mw_a = make(tmp_path)
    _, mw_b = make(tmp_path)
    ran = Tool()
    with caplog.at_level("WARNING"), thread(None):
        attempt(mw_a, ran, "run_shell", {"cmd": "ls"})
        attempt(mw_b, ran, "run_shell", {"cmd": "ls"})
        attempt(mw_a, ran, "run_shell", {"cmd": "ls"})
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2


def test_a_real_graph_run_without_a_thread_id_uses_the_default_thread_and_warns_once(
    tmp_path, caplog
):
    # Breaks if the real config of a graph run (no checkpointer, no thread_id) is read differently
    # from the hand-made one: two invokes, one warning, shared counts, even through a subagent.
    ran = []
    agent = build(
        guard_for(tmp_path),
        ran,
        [delegate("coder")],
        {"coder": [SHELL_CALL, SHELL_CALL]},
    )
    with caplog.at_level("WARNING"):
        first = agent.invoke({"messages": [("user", "go")]})
        with pytest.raises(GuardLoopError) as raised:
            agent.invoke({"messages": [("user", "go")]})
    assert tool_replies(first) == ["done"]
    assert raised.value.thread_id == "default"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert [("no thread_id" in r.getMessage()) for r in warnings] == [True]
    assert ran == []


# 8. The async path gives the same counts


def test_async_counts_threads_approvals_and_observe_like_the_sync_path(tmp_path, caplog):
    # Breaks if awrap_tool_call counts differently: wrong thread, an observe hit counted, an
    # approval kept for the wrong thread, or reordered keys read as a new call.
    _, mw = make(tmp_path)
    ran = Tool()

    async def call(name, args, call_id="c1"):
        try:
            return (await mw.awrap_tool_call(req(name, args, call_id), ran.acall)).content
        except GuardLoopError as exc:
            return exc

    async def scenario():
        out = {}
        with thread("x"):
            out["x1"] = await call("run_shell", {"cmd": "ls", "o": {"a": 1, "b": 2}})
            out["x2"] = await call("run_shell", {"o": {"b": 2, "a": 1}, "cmd": "ls"})
        with thread("y"):
            out["y1"] = await call("run_shell", {"cmd": "ls", "o": {"a": 1, "b": 2}})
            out["y_changed"] = await call("run_shell", {"cmd": "pwd"})
        with thread("x"):
            out["x3"] = await call("run_shell", {"cmd": "ls", "o": {"a": 1, "b": 2}})
            out["x_drop_before"] = await call("drop_table", {"name": "u"})
        with thread("x"):
            await call("ask_human", {})
            out["x_drop_after"] = await call("drop_table", {"name": "u"})
        with thread("y"):
            out["y_drop"] = await call("drop_table", {"name": "u"})
        with thread(None):
            out["d1"] = await call("run_shell", {"cmd": "ls"})
        with thread(None):
            out["d2"] = await call("run_shell", {"cmd": "ls"})
            out["d3"] = await call("run_shell", {"cmd": "ls"})
        return out

    with caplog.at_level("WARNING"):
        out = asyncio.run(scenario())
    assert [out[k] for k in ("x1", "x2", "y1", "y_changed", "x_drop_before")] == [
        SHELL,
        SHELL,
        SHELL,
        SHELL,
        DROP,
    ]
    assert isinstance(out["x3"], GuardLoopError) and out["x3"].thread_id == "x"
    assert (out["x_drop_after"], out["y_drop"], out["d1"], out["d2"]) == ("ran", DROP, SHELL, SHELL)
    assert isinstance(out["d3"], GuardLoopError) and out["d3"].thread_id == "default"
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    assert ran.ran == [{}, {"name": "u"}]


def test_async_observe_mode_never_counts(tmp_path):
    _, mw = make(tmp_path, OBSERVE_ONLY)
    ran = Tool()

    async def scenario():
        with thread("t1"):
            return [
                (
                    await mw.awrap_tool_call(req("run_shell", {"cmd": "ls"}, f"c{i}"), ran.acall)
                ).content
                for i in range(6)
            ]

    assert asyncio.run(scenario()) == ["ran"] * 6


def test_async_ainvoke_without_a_thread_id_shares_the_default_thread(tmp_path):
    # The no-thread-id case the spike left untested (FACTS section 13, Q4), on the async graph.
    ran = []
    agent = build(guard_for(tmp_path), ran, [SHELL_CALL], {})

    async def scenario():
        return [await agent.ainvoke({"messages": [("user", "go")]}) for _ in range(3)]

    with pytest.raises(GuardLoopError) as raised:
        asyncio.run(scenario())
    assert raised.value.thread_id == "default"
    assert ran == []


# 9. Concurrency: the counter is atomic


@pytest.fixture
def frequent_thread_switches():
    """Make the interpreter switch threads as often as it can, to expose a race."""
    before = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(before)


def hammer(mw, ran, thread_ids, calls_each, args):
    """One OS thread per entry of `thread_ids`, each making `calls_each` identical calls.

    Returns the list of (thread_id, outcome) over all calls."""
    barrier = threading.Barrier(len(thread_ids))
    results = []

    def work(tid):
        with thread(tid):
            barrier.wait()
            for i in range(calls_each):
                results.append((tid, attempt(mw, ran, "run_shell", args, f"c{i}")))

    workers = [threading.Thread(target=work, args=(tid,)) for tid in thread_ids]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    assert not any(worker.is_alive() for worker in workers)
    return results


def test_16_threads_sharing_one_thread_id_get_exactly_two_text_results(
    tmp_path, frequent_thread_switches
):
    # Breaks if the count's read-modify-write is not atomic: two callers could both see count 1
    # and give 3 text results, or both see 2 and none would be stopped on time (FR-017, E-5).
    _, mw = make(tmp_path)
    ran = Tool()
    results = hammer(mw, ran, ["shared"] * 16, 20, {"cmd": "ls"})
    texts = [o for _, o in results if isinstance(o, str)]
    stops = [o for _, o in results if isinstance(o, GuardLoopError)]
    assert (len(texts), len(stops)) == (2, 318)
    assert set(texts) == {SHELL}
    assert {s.thread_id for s in stops} == {"shared"}
    assert ran.ran == []


def test_16_threads_with_their_own_thread_id_get_two_text_results_each(
    tmp_path, frequent_thread_switches
):
    # Breaks if concurrent threads leak counts into each other: each thread is its own thread id.
    _, mw = make(tmp_path)
    ran = Tool()
    ids = [f"t{i}" for i in range(16)]
    results = hammer(mw, ran, ids, 20, {"cmd": "ls"})
    texts = Counter(tid for tid, o in results if isinstance(o, str))
    stops = Counter(tid for tid, o in results if isinstance(o, GuardLoopError))
    assert texts == dict.fromkeys(ids, 2)
    assert stops == dict.fromkeys(ids, 18)
    assert ran.ran == []


def test_concurrent_approval_in_one_thread_does_not_unblock_the_other_threads(
    tmp_path, frequent_thread_switches
):
    # Breaks if approvals race into a shared structure: 8 threads approve, 8 others never do.
    _, mw = make(tmp_path)
    ran = Tool()
    approving = [f"yes{i}" for i in range(8)]
    asking = [f"no{i}" for i in range(8)]
    barrier = threading.Barrier(16)
    outcomes = {}

    def work(tid, approves):
        with thread(tid):
            barrier.wait()
            if approves:
                run(mw, Tool(), "ask_human", {})
            outcomes[tid] = attempt(mw, ran, "drop_table", {"name": "users"})

    workers = [threading.Thread(target=work, args=(t, True)) for t in approving] + [
        threading.Thread(target=work, args=(t, False)) for t in asking
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    assert {t: outcomes[t] for t in approving} == dict.fromkeys(approving, "ran")
    assert {t: outcomes[t] for t in asking} == dict.fromkeys(asking, DROP)
    assert len(ran.ran) == 8


def test_async_tasks_on_one_thread_id_get_exactly_two_text_results(tmp_path):
    # The async twin of the 16-thread test: 16 tasks x 20 calls under one thread id.
    _, mw = make(tmp_path)
    ran = Tool()

    async def worker():
        out = []
        with thread("shared"):
            for i in range(20):
                try:
                    out.append(
                        (
                            await mw.awrap_tool_call(
                                req("run_shell", {"cmd": "ls"}, f"c{i}"), ran.acall
                            )
                        ).content
                    )
                except GuardLoopError as exc:
                    out.append(exc)
                await asyncio.sleep(0)
        return out

    async def scenario():
        return [r for batch in await asyncio.gather(*(worker() for _ in range(16))) for r in batch]

    results = asyncio.run(scenario())
    assert sum(isinstance(r, str) for r in results) == 2
    assert sum(isinstance(r, GuardLoopError) for r in results) == 318
    assert ran.ran == []
