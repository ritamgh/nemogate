import asyncio

import pytest
from langchain_core.messages import AIMessage

from common.llm import StubChatModel, get_model
from common.tests.conftest import read_log


def test_default_stub_names_the_role_and_is_deterministic(spend_log):
    # Breaks if get_model returns something other than the stub when not live, or replies vary.
    model = get_model("sut")
    assert isinstance(model, StubChatModel)
    first = model.invoke("anything")
    second = model.invoke("something else")
    assert first.content == second.content == "stub reply from sut"
    assert first.usage_metadata == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert read_log(spend_log) == []


def test_scripted_replies_come_back_in_order_including_tool_calls():
    # Breaks if scripted replies are reordered, strings are not wrapped, or tool_calls are lost.
    tool_msg = AIMessage(
        content="",
        tool_calls=[{"name": "ls", "args": {"path": "."}, "id": "call_1", "type": "tool_call"}],
    )
    model = get_model("planner", stub_responses=["one", tool_msg, "three"])
    assert model.invoke("a").content == "one"
    second = model.invoke("b")
    assert second.tool_calls[0]["name"] == "ls"
    assert second.tool_calls[0]["args"] == {"path": "."}
    assert second.tool_calls[0]["id"] == "call_1"
    assert model.invoke("c").content == "three"


def test_exhausted_script_raises_a_clear_error():
    # Breaks if the stub silently repeats or returns empty when the script runs out.
    model = get_model("helper", stub_responses=["only"])
    model.invoke("a")
    with pytest.raises(RuntimeError, match="stub_responses exhausted after 1"):
        model.invoke("b")


def test_bind_tools_returns_a_working_model():
    # Breaks if Deep Agents' bind_tools call fails or loses the scripted replies.
    model = get_model("sut", stub_responses=["bound reply"])
    bound = model.bind_tools([{"type": "function", "function": {"name": "f", "parameters": {}}}])
    assert bound.invoke("hi").content == "bound reply"


def test_ainvoke_works_and_shares_the_script():
    # Breaks if the async path is unsupported or restarts the script.
    model = get_model("sut", stub_responses=["first", "second"])
    assert model.invoke("x").content == "first"
    assert asyncio.run(model.ainvoke("y")).content == "second"


def test_unknown_role_is_rejected():
    # Breaks if a typo'd role silently gets a stub.
    with pytest.raises(ValueError):
        get_model("judge")
