"""The guard on a fake Deep Agents app: create_deep_agent, a scripted chat model, the app's own
tool and its own subagents, and nothing from refapp (PRD SC-001, FR-002, FR-004).

The app's only file-writing tool is `save_note`, a tool the rules file maps (path_arg, op: write).
It counts how often it really ran, so "blocked" means "ran 0 times", not "returned an error".
"""

import asyncio
import json
import socket
from typing import Any

import pytest
from deepagents import create_deep_agent
from deepagents.middleware.subagents import SubAgent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

import nemogate.common.llm
from nemogate import gate
from nemogate.gate.errors import GuardLoopError
from nemogate.gate.guard import Guard

RULES = """\
version: 1
tools:
  save_note: {path_arg: path, op: write}
rules:
  - id: no-migrations
    text: Never touch the migrations folder.
    check:
      path_forbidden: {glob: "migrations/**", ops: [write, delete]}
  - id: no-shell
    text: Do not run shell commands.
    check:
      tool_forbidden: {name: run_shell}
"""
BLOCKED = "Blocked by rule no-migrations: Never touch the migrations folder."
REPIN = (
    "\n\nRules from the user that apply to this work:\n"
    "- Never touch the migrations folder.\n"
    "- Do not run shell commands."
)
BAD = {"path": "migrations/001.sql", "text": "drop table"}
GOOD = {"path": "app/main.py", "text": "print('hi')"}

RAN: list[dict] = []  # every real run of save_note, in order


@tool
def save_note(path: str, text: str) -> str:
    """Save a note at a path (the app's own tool, not Deep Agents' write_file)."""
    RAN.append({"path": path, "text": text})
    return f"saved {path}"


def _task(sub: str, text: str) -> tuple[str, dict]:
    return "task", {"subagent_type": sub, "description": text}


class ScriptedChat(BaseChatModel):
    """Replies with the next scripted tool call of the agent's script, then with plain text.

    The script is chosen by the agent's first human message up to its first blank line (a
    delegation's description, with or without the rules appended), and the step by how many tool
    results the agent already holds. `starts` lists every first human message as the model got it.
    """

    scripts: dict
    starts: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChat":
        return self

    def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kw: Any):
        first = str(next(m for m in messages if isinstance(m, HumanMessage)).content)
        if sum(isinstance(m, HumanMessage) for m in messages) == 1 and not any(
            isinstance(m, ToolMessage) for m in messages
        ):
            self.starts.append(first)
        key = first.split("\n\n")[0]
        steps = self.scripts[key]
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < len(steps):
            name, args = steps[done]
            reply = AIMessage(
                content="", tool_calls=[{"name": name, "args": args, "id": f"{key}-{done}"}]
            )
        else:
            reply = AIMessage(content=f"done: {key}")
        return ChatResult(generations=[ChatGeneration(message=reply)])


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Every test runs with no model factory, no network and no leftover tool runs."""
    RAN.clear()
    attempts: list[tuple] = []

    def no_connect(self, address):
        attempts.append(address)
        raise OSError("the guard test is offline")

    def no_model(*args, **kwargs):
        attempts.append(("get_model", args))
        raise AssertionError("the guard asked for a model")

    monkeypatch.setattr(socket.socket, "connect", no_connect)
    monkeypatch.setattr(nemogate.common.llm, "get_model", no_model)
    monkeypatch.setattr(gate, "get_model", no_model, raising=False)
    for key in ("OPENAI_API_KEY", "NEBIUS_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return attempts


def make_guard(tmp_path, text=RULES):
    rules = tmp_path / "rules.yaml"
    rules.write_text(text, encoding="utf-8")
    return Guard.from_yaml(rules, trace_path=tmp_path / "trace.jsonl")


def trace_of(tmp_path):
    lines = (tmp_path / "trace.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def blocks_of(tmp_path):
    """(agent, tool, rule, reason) of each block the guard wrote, in order."""
    return [
        (e["agent"], e["tool"], e["constraint_id"], e["reason"])
        for e in trace_of(tmp_path)
        if e["type"] == "gate_block"
    ]


def build(model, guard, subagents=()):
    return create_deep_agent(
        model=model,
        tools=[save_note],
        middleware=guard.middleware(),
        subagents=list(subagents),
        checkpointer=InMemorySaver(),
    )


def guarded_gp(model, guard):
    """The quickstart pattern: a declared general-purpose subagent that carries the guard."""
    return SubAgent(
        name="general-purpose",
        description="General-purpose agent for multi-step work.",
        system_prompt="Do the work you are given.",
        model=model,
        middleware=guard.middleware(),
    )


def config(thread="app-1"):
    return {"configurable": {"thread_id": thread}, "recursion_limit": 40}


def start(text="go"):
    return {"messages": [HumanMessage(content=text)]}


def replies(out):
    return [(m.content, m.status) for m in out["messages"] if isinstance(m, ToolMessage)]


# 1. The main agent


def test_a_rule_breaking_write_is_blocked_in_the_main_agent_and_the_tool_runs_zero_times(tmp_path):
    # Fails if Guard.middleware() is not reaching the main agent's tool node, or blocks after
    # the call: RAN would then hold the migrations write.
    guard = make_guard(tmp_path)
    model = ScriptedChat(
        scripts={
            "go": [
                ("save_note", BAD),
                ("save_note", GOOD),
                ("write_file", {"file_path": "/migrations/x.sql", "content": "drop"}),
            ]
        }
    )
    out = build(model, guard).invoke(start(), config())
    assert replies(out) == [
        (BLOCKED, "error"),
        ("saved app/main.py", "success"),
        (BLOCKED, "error"),
    ]
    assert RAN == [GOOD]
    assert out.get("files", {}) == {}  # the virtual-path write_file never created a file
    assert blocks_of(tmp_path) == [
        ("main", "save_note", "no-migrations", BLOCKED),
        ("main", "write_file", "no-migrations", BLOCKED),
    ]


# 2. Subagents: protected only with the middleware in their spec


def test_a_declared_general_purpose_subagent_with_the_guard_is_protected(tmp_path):
    # Fails if the SubAgent spec's middleware list is not applied to the subagent's own tool node.
    guard = make_guard(tmp_path)
    model = ScriptedChat(
        scripts={"go": [_task("general-purpose", "DO:gp")], "DO:gp": [("save_note", BAD)]}
    )
    out = build(model, guard, [guarded_gp(model, guard)]).invoke(start(), config())
    assert RAN == []
    assert out["messages"][-1].content == "done: go"
    assert blocks_of(tmp_path) == [("general-purpose", "save_note", "no-migrations", BLOCKED)]


@pytest.mark.parametrize("subagent", ["plain", "general-purpose"])
def test_a_subagent_without_the_guard_is_not_protected(tmp_path, subagent):
    # Documents the limit (docs/FACTS.md section 13): middleware= on create_deep_agent reaches the
    # main agent only. Fails, and so flags a change in Deep Agents, if the default general-purpose
    # or a declared subagent without a middleware key starts inheriting the guard.
    guard = make_guard(tmp_path)
    model = ScriptedChat(
        scripts={"go": [_task(subagent, "DO:sub")], "DO:sub": [("save_note", BAD)]}
    )
    declared = [
        SubAgent(
            name="plain",
            description="A declared subagent with no middleware key.",
            system_prompt="Do the work you are given.",
            model=model,
        )
    ]
    out = build(model, guard, declared).invoke(start(), config())
    assert RAN == [BAD]  # the rule-breaking write ran
    assert out["messages"][-1].content == "done: go"
    assert blocks_of(tmp_path) == []


# 3. Re-pin at delegation


def test_a_delegation_reaches_the_subagent_with_the_rule_text_appended(tmp_path):
    # Fails if the task call is passed on unchanged: the subagent would see "Add a field." alone.
    guard = make_guard(tmp_path)
    model = ScriptedChat(
        scripts={
            "go": [_task("general-purpose", "Add a field.")],
            "Add a field.": [],
        }
    )
    build(model, guard, [guarded_gp(model, guard)]).invoke(start(), config())
    assert model.starts == ["go", "Add a field." + REPIN]


def test_a_blocked_write_in_the_subagent_and_the_re_pin_share_one_trace(tmp_path):
    # Fails if the main agent's and the subagent's middleware instances do not share the guard's
    # trace and step counter: steps would restart or lines would go missing.
    guard = make_guard(tmp_path)
    model = ScriptedChat(
        scripts={
            "go": [_task("general-purpose", "Add a field.")],
            "Add a field.": [("save_note", BAD)],
        }
    )
    build(model, guard, [guarded_gp(model, guard)]).invoke(start(), config())
    assert [(e["step"], e["type"], e["agent"]) for e in trace_of(tmp_path)] == [
        (0, "tool_call", "main"),
        (1, "gate_block", "general-purpose"),
    ]


# 4. No model, no network


def test_a_whole_run_makes_no_get_model_call_and_no_network_connect(tmp_path, offline):
    # Fails if the guard (or a path it takes) asks the model factory for a model or opens a
    # socket: every attempt is recorded by the autouse fixture and would be listed here.
    guard = make_guard(tmp_path)
    model = ScriptedChat(
        scripts={
            "go": [("save_note", BAD), _task("general-purpose", "DO:gp"), ("save_note", GOOD)],
            "DO:gp": [("save_note", BAD)],
        }
    )
    out = build(model, guard, [guarded_gp(model, guard)]).invoke(start(), config())
    assert out["messages"][-1].content == "done: go"
    assert len(blocks_of(tmp_path)) == 2
    assert offline == []


# 5. Sync and async


def scenario(tmp_path, call):
    tmp_path.mkdir()
    RAN.clear()
    guard = make_guard(tmp_path)
    model = ScriptedChat(
        scripts={
            "go": [
                ("save_note", BAD),
                ("save_note", GOOD),
                _task("general-purpose", "DO:gp"),
                ("run_shell", {"cmd": "ls"}),
            ],
            "DO:gp": [("save_note", BAD)],
        }
    )
    out = call(build(model, guard, [guarded_gp(model, guard)]))
    seen = [
        (e["type"], e["agent"], e.get("tool"), e.get("constraint_id")) for e in trace_of(tmp_path)
    ]
    return replies(out), list(RAN), blocks_of(tmp_path), seen, model.starts


def test_invoke_and_ainvoke_block_the_same_calls_the_same_way(tmp_path):
    # Fails if the async hook (awrap_tool_call) decides differently from the sync one.
    sync = scenario(tmp_path / "sync", lambda a: a.invoke(start(), config("sync")))
    asyn = scenario(tmp_path / "async", lambda a: asyncio.run(a.ainvoke(start(), config("async"))))
    assert sync == asyn
    replies_, ran, blocks, _, _ = sync
    assert [r for r in replies_ if r[1] == "error"] == [
        (BLOCKED, "error"),
        ("Blocked by rule no-shell: Do not run shell commands.", "error"),
    ]
    assert ran == [GOOD]
    assert blocks == [
        ("main", "save_note", "no-migrations", BLOCKED),
        ("general-purpose", "save_note", "no-migrations", BLOCKED),
        ("main", "run_shell", "no-shell", "Blocked by rule no-shell: Do not run shell commands."),
    ]


# 6. The retry limit through a real graph


def test_the_third_identical_blocked_call_raises_guard_loop_error_through_the_graph(tmp_path):
    # Fails if the loop limit is not enforced (or is swallowed by the tool node) in a real graph:
    # the agent would go on to a fourth call or finish normally.
    guard = make_guard(tmp_path)
    model = ScriptedChat(scripts={"go": [("save_note", BAD)] * 4})
    agent = build(model, guard)
    with pytest.raises(GuardLoopError, match="no-migrations") as raised:
        agent.invoke(start(), config("loop"))
    assert (raised.value.rule_id, raised.value.tool, raised.value.thread_id) == (
        "no-migrations",
        "save_note",
        "loop",
    )
    assert RAN == []
    assert [b[2] for b in blocks_of(tmp_path)] == ["no-migrations"] * 3


def test_the_third_identical_blocked_call_raises_in_the_async_graph_too(tmp_path):
    guard = make_guard(tmp_path)
    model = ScriptedChat(scripts={"go": [("save_note", BAD)] * 4})
    agent = build(model, guard)
    with pytest.raises(GuardLoopError, match="no-migrations"):
        asyncio.run(agent.ainvoke(start(), config("loop-async")))
    assert RAN == []
    assert len(blocks_of(tmp_path)) == 3
