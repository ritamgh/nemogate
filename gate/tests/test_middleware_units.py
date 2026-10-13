import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from gate.middleware import _with_text, main_agent_name


def test_with_text_replaces_a_tool_message_content():
    msg = ToolMessage(content="old", tool_call_id="c1", name="task")
    out = _with_text(msg, "new")
    assert (out.content, out.tool_call_id, out.name) == ("new", "c1", "task")


def test_with_text_keeps_the_rest_of_a_command_update():
    msg = ToolMessage(content="old", tool_call_id="c1", name="task")
    result = Command(update={"files": {"a": 1}, "messages": [msg]})
    out = _with_text(result, "new")
    assert out.update["files"] == {"a": 1}
    assert [m.content for m in out.update["messages"]] == ["new"]


def test_with_text_replaces_a_plain_string_result():
    assert _with_text("error: unknown subagent", "x") == "x"


def test_with_text_refuses_a_command_it_cannot_place_text_in():
    # Breaks if an unexpected result shape is silently replaced by a string, dropping the
    # subagent's state update while the trace claims the mutated reply was delivered.
    with pytest.raises(TypeError, match="cannot place"):
        _with_text(Command(update={"files": {}}), "x")


@pytest.mark.parametrize(
    ("boundary", "main"),
    [
        ("delegation:lead->worker", "lead"),
        ("return:worker->lead", "lead"),
        ("compaction:worker", "worker"),
    ],
)
def test_main_agent_name(boundary, main):
    assert main_agent_name(boundary) == main
