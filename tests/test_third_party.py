"""The guard on a Deep Agents app that is not the reference app (examples/third_party).

The app has its own tools and its own rules file and takes only `Guard` from nemogate. Every
test runs a real create_deep_agent graph with a scripted chat model and no network. `calls` is
what the app's tools really ran, so "blocked" means "not in calls", not "returned an error".
"""

import ast
import re
import socket
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import nemogate.common.llm
from nemogate import Guard, GuardLoopError

# examples/ is scanned by replay's audit tests: keep a bytecode folder out of it.
_write_bytecode, sys.dont_write_bytecode = sys.dont_write_bytecode, True
try:
    from examples.third_party.app import RULES, build_agent
finally:
    sys.dont_write_bytecode = _write_bytecode

ROOT = Path(__file__).resolve().parent.parent
PAYMENT_BLOCK = (
    "Blocked by rule payment-limit: Never send a payment of more than 100. "
    "(argument 'amount' is 250, the rule requires <= 100)"
)
DELETE_BLOCK = (
    "Blocked by rule keep-protected-records: Never delete anything under records/protected."
)
REPIN = (
    "\n\nRules from the user that apply to this work:\n"
    "- Never send a payment of more than 100.\n"
    "- Never delete anything under records/protected."
)
BIG = ("send_payment", {"amount": 250, "to": "bob"})
SMALL = ("send_payment", {"amount": 50, "to": "bob"})
PROTECTED = ("delete_record", {"path": "records/protected/a.txt"})
OTHER = ("delete_record", {"path": "records/other.txt"})


class ScriptedChat(BaseChatModel):
    """Replies with the next scripted tool call of the agent's script, then with plain text.

    The script is chosen by the agent's first human message up to its first blank line, and the
    step by how many tool results the agent holds. `starts` lists each agent's first human
    message as it got it; `invocations` counts every call of the model."""

    scripts: dict
    starts: list = []
    invocations: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChat":
        return self

    def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kw: Any):
        self.invocations.append(len(messages))
        first = str(next(m for m in messages if isinstance(m, HumanMessage)).content)
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done == 0:
            self.starts.append(first)
        key = first.split("\n\n")[0]
        steps = self.scripts[key]
        if done < len(steps):
            name, args = steps[done]
            reply = AIMessage(
                content="", tool_calls=[{"name": name, "args": args, "id": f"{key}-{done}"}]
            )
        else:
            reply = AIMessage(content=f"done: {key}")
        return ChatResult(generations=[ChatGeneration(message=reply)])


def delegate(text):
    return "task", {"subagent_type": "general-purpose", "description": text}


def run(scripts, thread="t1"):
    """Run the app on the script's "go" start; returns (calls, model, final state)."""
    calls: list = []
    model = ScriptedChat(scripts=scripts, starts=[], invocations=[])
    agent = build_agent(model, RULES, calls)
    out = agent.invoke(
        {"messages": [HumanMessage(content="go")]},
        {"configurable": {"thread_id": thread}, "recursion_limit": 40},
    )
    return calls, model, out


def tool_replies(out):
    return [(m.content, m.status) for m in out["messages"] if isinstance(m, ToolMessage)]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No model factory, no network, no key: a guard that tried either would be listed."""
    attempts: list = []

    def no_connect(self, address):
        attempts.append(address)
        raise OSError("offline")

    def no_model(*args, **kwargs):
        attempts.append(("get_model", args))
        raise AssertionError("asked for a model")

    monkeypatch.setattr(socket.socket, "connect", no_connect)
    monkeypatch.setattr(nemogate.common.llm, "get_model", no_model)
    for key in ("OPENAI_API_KEY", "NEBIUS_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return attempts


# a. the main agent


# Fails if Guard.middleware() does not reach the main agent's tool node, or blocks after the
# call: the 250 payment would be in calls.
def test_a_payment_over_the_limit_is_blocked_and_one_under_it_runs():
    calls, _, out = run({"go": [BIG, SMALL]})
    assert tool_replies(out) == [(PAYMENT_BLOCK, "error"), ("sent 50.0 to bob", "success")]
    assert calls == [(50.0, "bob")]


# b. a subagent


# Fails if the declared general-purpose subagent does not carry the guard, or the delegation
# passes the task text on unchanged (no rule text for the subagent to see).
def test_the_same_payment_made_by_the_general_purpose_subagent_is_blocked_and_repinned():
    calls, model, out = run({"go": [delegate("Pay bob.")], "Pay bob.": [BIG]})
    assert calls == []
    assert model.starts == ["go", "Pay bob." + REPIN]
    assert out["messages"][-1].content == "done: go"


# c. a file-style tool through the tools map


# Fails if the tools map is not applied to the app's own delete_record, or the glob does not
# match: the protected delete would run (or the other one be blocked).
def test_a_delete_under_the_protected_folder_is_blocked_and_another_delete_runs():
    calls, _, out = run({"go": [PROTECTED, OTHER]})
    assert tool_replies(out) == [(DELETE_BLOCK, "error"), ("deleted records/other.txt", "success")]
    assert calls == ["records/other.txt"]


# d. no model calls from the guard


# Fails if the guard makes a model call of its own (a helper call while blocking or re-pinning):
# the model would be invoked more often than the app's own script needs, or get_model or a
# socket would be hit (the autouse fixture lists any attempt).
def test_the_run_makes_only_the_apps_own_model_calls_and_never_asks_for_a_model(offline):
    calls, model, out = run({"go": [delegate("Pay bob."), BIG], "Pay bob.": [BIG]})
    assert calls == []
    # main: task, big payment, final reply; subagent: big payment, final reply
    assert len(model.invocations) == 5
    assert offline == []


# Fails if model-client code gets into the guard's files: `get_model(` is called only in the
# pipeline's gate/__init__.py (the experiment gate), the OpenAI client is imported only by
# operators.py, and the guard's own files (guard, middleware, policy, state, trace) have neither.
def test_only_the_experiment_gate_and_its_operators_touch_a_model_client():
    gate = ROOT / "src" / "nemogate" / "gate"
    files = sorted(gate.glob("*.py"))
    asks = {f.name for f in files if re.search(r"\bget_model\(", f.read_text())}
    clients = {f.name for f in files if re.search(r"ChatOpenAI|langchain_openai", f.read_text())}
    assert asks == {"__init__.py"}
    assert clients == {"operators.py"}


# Fails if the example app starts importing the pipeline (refapp, replay, gate internals):
# it would no longer show what an outside developer gets from `from nemogate import Guard`.
def test_the_example_app_imports_nothing_from_nemogate_but_guard():
    tree = ast.parse((ROOT / "examples" / "third_party" / "app.py").read_text())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [(a.name, None) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [(node.module, a.name) for a in node.names]
    own = [i for i in imported if i[0].split(".")[0] in {"nemogate", "refapp", "replay"}]
    assert own == [("nemogate", "Guard")]


# e. the retry limit


# Fails if the loop limit is not enforced through the graph (the agent would go on to a fourth
# call), or is counted across threads (the second thread would raise on its first block).
def test_the_third_identical_blocked_call_raises_and_another_thread_starts_over():
    calls: list = []
    scripts = {"go": [BIG] * 4, "go-again": [BIG] * 2}
    model = ScriptedChat(scripts=scripts, starts=[], invocations=[])
    agent = build_agent(model, RULES, calls)
    with pytest.raises(GuardLoopError, match="payment-limit") as raised:
        agent.invoke(
            {"messages": [HumanMessage(content="go")]}, {"configurable": {"thread_id": "t1"}}
        )
    assert (raised.value.rule_id, raised.value.tool, raised.value.thread_id) == (
        "payment-limit",
        "send_payment",
        "t1",
    )
    out = agent.invoke(
        {"messages": [HumanMessage(content="go-again")]}, {"configurable": {"thread_id": "t2"}}
    )
    assert tool_replies(out) == [(PAYMENT_BLOCK, "error")] * 2
    assert out["messages"][-1].content == "done: go-again"
    assert calls == []


# f. no rules file


# Fails if a missing rules file is ignored (an unguarded app) or the error does not name it.
def test_a_missing_rules_file_is_an_error_that_names_the_path(tmp_path):
    missing = tmp_path / "no-such-rules.yaml"
    with pytest.raises(FileNotFoundError, match=re.escape(str(missing))):
        Guard.from_yaml(missing)
    with pytest.raises(FileNotFoundError, match=re.escape(str(missing))):
        build_agent(ScriptedChat(scripts={}), missing, [])
