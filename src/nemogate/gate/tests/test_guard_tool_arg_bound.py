"""tool_arg_bound in the guard: the rules file loads it, and a call whose argument fails the
comparison is blocked before the tool runs, with the reason in the block result."""

import json
from contextlib import contextmanager

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolCallRequest

from nemogate.gate.errors import GuardLoopError
from nemogate.gate.guard import Guard
from nemogate.gate.policy import blocking_constraint, checkable_before_the_call
from nemogate.schemas.ledger import Ledger, ToolArgBound
from nemogate.schemas.trace import ToolCall

CAP = """\
version: 1
rules:
  - id: refund-cap
    text: Refunds are capped at 100.
    check:
      tool_arg_bound: {tool: refund, arg: amount, op: "<=", value: 100}
"""
REGION = """\
version: 1
rules:
  - id: eu-only
    text: Only the eu region may be touched.
    check:
      tool_arg_bound: {tool: deploy, arg: region, op: "==", value: eu}
"""
RULE = "Refunds are capped at 100."


class Tool:
    """A stand-in tool that records how often the real tool ran."""

    def __init__(self):
        self.ran = []

    def __call__(self, request):
        call = request.tool_call
        self.ran.append(call["args"])
        return ToolMessage(
            content="ran", tool_call_id=call["id"], name=call["name"], status="success"
        )


def req(name, args, call_id="c1"):
    call = {"name": name, "args": args, "id": call_id, "type": "tool_call"}
    return ToolCallRequest(tool_call=call, tool=None, state={}, runtime=None)


@contextmanager
def thread(thread_id="t1"):
    config = {"configurable": {"thread_id": thread_id}, "metadata": {"lc_agent_name": "coder"}}
    token = var_child_runnable_config.set(config)
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


def make(tmp_path, text=CAP, **kwargs):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    guard = Guard.from_yaml(path, **kwargs)
    (mw,) = guard.middleware()
    return mw


def run(mw, tool_, name, args, call_id="c1"):
    return mw.wrap_tool_call(req(name, args, call_id), tool_)


def blocked(reason):
    return f"Blocked by rule refund-cap: {RULE} ({reason})"


# The decision


def test_an_amount_over_the_cap_is_blocked_and_the_tool_runs_zero_times(tmp_path):
    # Breaks if tool_arg_bound is not in the pre-call set (the call would run) or the reason
    # does not say what failed.
    mw, tool_ = make(tmp_path), Tool()
    with thread():
        result = run(mw, tool_, "refund", {"amount": 250})
    assert tool_.ran == []
    assert (result.status, result.tool_call_id) == ("error", "c1")
    assert result.content == blocked("argument 'amount' is 250, the rule requires <= 100")


def test_an_amount_within_the_cap_runs_the_tool(tmp_path):
    # Breaks if the op is read as the violation: <= 100 would then block 50.
    mw, tool_ = make(tmp_path), Tool()
    with thread():
        result = run(mw, tool_, "refund", {"amount": 50})
    assert tool_.ran == [{"amount": 50}]
    assert result.content == "ran"


def test_a_call_that_leaves_the_argument_out_is_blocked_and_says_missing(tmp_path):
    # Breaks if a missing argument fails open.
    mw, tool_ = make(tmp_path), Tool()
    with thread():
        result = run(mw, tool_, "refund", {"currency": "eur"})
    assert tool_.ran == []
    assert result.content == blocked("argument 'amount' is missing")


@pytest.mark.parametrize(
    ("sent", "kind"), [(None, "null"), ("50", "text"), (True, "boolean"), ([50], "other")]
)
def test_a_null_or_wrongly_typed_argument_is_blocked_as_a_type_mismatch(tmp_path, sent, kind):
    # Breaks if None, the text "50" or True (a bool is not a number) slips through.
    mw, tool_ = make(tmp_path), Tool()
    with thread():
        result = run(mw, tool_, "refund", {"amount": sent})
    assert tool_.ran == []
    assert result.content == blocked(
        f"argument 'amount': type does not match (got {kind}, the rule needs a number)"
    )


def test_a_text_rule_lets_the_equal_value_run_and_blocks_another(tmp_path):
    mw, tool_ = make(tmp_path, REGION), Tool()
    with thread():
        allowed = run(mw, tool_, "deploy", {"region": "eu"}, "c1")
        refused = run(mw, tool_, "deploy", {"region": "us"}, "c2")
    assert allowed.content == "ran"
    assert tool_.ran == [{"region": "eu"}]
    assert refused.content == (
        "Blocked by rule eu-only: Only the eu region may be touched. "
        "(argument 'region' is 'us', the rule requires == 'eu')"
    )


def test_other_tools_are_untouched(tmp_path):
    # Breaks if the rule fires on a tool it does not name.
    mw, tool_ = make(tmp_path), Tool()
    with thread():
        result = run(mw, tool_, "lookup", {"amount": 9999})
    assert result.content == "ran"


def test_the_third_identical_blocked_call_raises_guard_loop_error(tmp_path):
    mw, tool_ = make(tmp_path), Tool()
    with thread():
        run(mw, tool_, "refund", {"amount": 250})
        run(mw, tool_, "refund", {"amount": 250})
        with pytest.raises(GuardLoopError, match="refund-cap") as raised:
            run(mw, tool_, "refund", {"amount": 250})
    assert (raised.value.rule_id, raised.value.tool) == ("refund-cap", "refund")
    assert tool_.ran == []


def test_observe_mode_logs_a_would_block_with_the_reason_and_lets_the_call_run(tmp_path):
    trace = tmp_path / "trace.jsonl"
    text = CAP.replace("    check:", "    mode: observe\n    check:")
    mw, tool_ = make(tmp_path, text, trace_path=trace), Tool()
    with thread():
        results = [run(mw, tool_, "refund", {"amount": 250}, f"c{i}") for i in range(4)]
    assert [r.content for r in results] == ["ran"] * 4  # and no GuardLoopError on the 3rd
    first = json.loads(trace.read_text().splitlines()[0])
    assert (first["type"], first["constraint_id"], first["reason"]) == (
        "gate_block",
        "refund-cap",
        "would block (observe mode): Refunds are capped at 100. "
        "(argument 'amount' is 250, the rule requires <= 100)",
    )


def test_a_block_is_written_to_the_trace_with_the_same_reason(tmp_path):
    trace = tmp_path / "trace.jsonl"
    mw, tool_ = make(tmp_path, trace_path=trace), Tool()
    with thread():
        run(mw, tool_, "refund", {"amount": 250})
    (line,) = [json.loads(x) for x in trace.read_text().splitlines()]
    assert (line["type"], line["tool"], line["reason"]) == (
        "gate_block",
        "refund",
        blocked("argument 'amount' is 250, the rule requires <= 100"),
    )


# The policy the block_only and gated arms share


def test_the_oracle_and_the_policy_agree_on_a_blocked_call():
    check = {"type": "tool_arg_bound", "tool": "refund", "arg": "amount", "op": "<=", "value": 100}
    entry = {
        "id": "C-001",
        "text": "rule",
        "kind": "constraint",
        "source": "user",
        "authority": "binding",
        "scope": ["worker"],
        "origin": "declared",
        "check": check,
    }
    ledger = Ledger.model_validate({"version": 1, "constraints": [entry]})
    stamp = {
        "run_id": "r-1",
        "arm": "block_only",
        "operator": "drop",
        "model_id": "m",
        "safe_config_hash": "h",
        "ledger_hash": "l",
    }

    def attempt(amount):
        return ToolCall(**stamp, step=3, agent="worker", tool="refund", args={"amount": amount})

    assert checkable_before_the_call(ledger.constraints[0])
    assert blocking_constraint(ledger, [], attempt(250)) is not None
    assert blocking_constraint(ledger, [], attempt(50)) is None


# A real graph


@tool
def refund(amount: float) -> str:
    """Refund an amount (the app's own tool with a numeric argument)."""
    RAN.append(amount)
    return f"refunded {amount}"


RAN: list = []


class ScriptedChat(BaseChatModel):
    calls: list

    @property
    def _llm_type(self):
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


def test_a_real_deep_agent_never_runs_a_refund_over_the_cap(tmp_path):
    # Breaks if the guard does not reach the tool node of a real graph, or blocks after the call:
    # RAN would then hold 250.0.
    RAN.clear()
    path = tmp_path / "rules.yaml"
    path.write_text(CAP, encoding="utf-8")
    guard = Guard.from_yaml(path)
    model = ScriptedChat(
        calls=[("refund", {"amount": 250}), ("refund", {"amount": 40}), ("refund", {})]
    )
    agent = create_deep_agent(model=model, tools=[refund], middleware=guard.middleware())
    out = agent.invoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "g-1"}})
    replies = [(m.content, m.status) for m in out["messages"] if isinstance(m, ToolMessage)]
    assert RAN == [40.0]
    assert replies == [
        (blocked("argument 'amount' is 250, the rule requires <= 100"), "error"),
        ("refunded 40.0", "success"),
        (blocked("argument 'amount' is missing"), "error"),
    ]
    assert out["messages"][-1].content == "done"


# Loading the rules file


def load(tmp_path, check):
    path = tmp_path / "rules.yaml"
    head = "version: 1\nrules:\n  - id: r1\n    text: A rule.\n    check:\n"
    path.write_text(f"{head}      tool_arg_bound: {check}\n", encoding="utf-8")
    return path


def test_a_tool_arg_bound_rule_loads_with_its_four_fields(tmp_path):
    # Breaks if the loader still rejects the type, or drops a field on the way to the model.
    guard = Guard.from_yaml(load(tmp_path, '{tool: refund, arg: amount, op: ">=", value: 2.5}'))
    assert guard.rules[0].constraint.check == ToolArgBound(
        tool="refund", arg="amount", op=">=", value=2.5
    )


@pytest.mark.parametrize(
    ("check", "field"),
    [
        ('{tool: refund, arg: amount, op: "<=", value: 1, limit: 2}', "limit"),
        ('{tool: refund, op: "<=", value: 1}', "arg"),
        ('{tool: refund, arg: amount, op: "=<", value: 1}', "op"),
        ('{tool: refund, arg: amount, op: "<=", value: [1]}', "value"),
        ('{tool: refund, arg: amount, op: "<=", value: true}', "value"),
        ('{tool: refund, arg: amount, op: "<=", value: "100"}', "value"),
        ('{tool: refund, arg: amount, op: ">", value: eu}', "value"),
        ("{tool: refund, arg: amount, op: '<='}", "value"),
        ('{tool: refund, arg: "", op: "<=", value: 1}', "arg"),
    ],
)
def test_a_bad_tool_arg_bound_names_the_file_the_rule_and_the_field(tmp_path, check, field):
    # Breaks if a malformed bound loads (a rule that guards nothing or blocks everything).
    path = load(tmp_path, check)
    with pytest.raises(ValueError) as raised:
        Guard.from_yaml(path)
    message = str(raised.value)
    assert str(path) in message
    assert "'r1'" in message
    assert f"'{field}'" in message or f"tool_arg_bound.{field}" in message
