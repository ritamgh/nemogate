import pytest
from langchain_core.messages import HumanMessage

from common.llm import get_model
from gate.operators import DROP_PROMPT, make_operator
from schemas.enums import Operator


class Spy:
    """Wraps a stub model and records the prompts it receives."""

    def __init__(self, replies):
        self.model = get_model("helper", stub_responses=replies)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.model.invoke([HumanMessage(prompt)])


def test_none_operator_is_no_mutation():
    assert make_operator(Operator.none, "rule", Spy([])) is None


def test_drop_sends_rule_and_message_and_returns_the_stripped_reply():
    spy = Spy(["  Build the export.  "])
    drop = make_operator(Operator.drop, "Never touch vendor/", spy)
    message = "Build the export. Never touch vendor/."
    assert drop(message) == "Build the export."
    assert spy.prompts == [DROP_PROMPT.format(rule="Never touch vendor/", message=message)]


def test_drop_prompt_names_no_task_or_repo():
    # Generality (AGENTS.md rule 5): the prompt describes the method only.
    for word in ("migrations", "RISK", "planner", "coder", "reviewer"):
        assert word not in DROP_PROMPT


def test_empty_reply_raises_instead_of_dropping_everything():
    drop = make_operator(Operator.drop, "rule", Spy([""]))
    with pytest.raises(RuntimeError, match="empty"):
        drop("message")


@pytest.mark.parametrize("op", [Operator.hedge, Operator.compress_100])
def test_week_two_operators_are_not_built_yet(op):
    with pytest.raises(NotImplementedError):
        make_operator(op, "rule", Spy([]))
