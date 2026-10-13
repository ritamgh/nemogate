import pytest
from langchain_core.messages import HumanMessage

from common.llm import get_model
from gate.operators import (
    COMPRESS_PROMPT,
    DROP_PROMPT,
    DROP_RETURN_PROMPT,
    HEDGE_PROMPT,
    HEDGE_RETURN_PROMPT,
    first_words,
    make_operator,
)
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


@pytest.mark.parametrize(
    ("op", "boundary", "prompt"),
    [
        (Operator.drop, "delegation", DROP_PROMPT),
        (Operator.drop, "return", DROP_RETURN_PROMPT),
        (Operator.hedge, "delegation", HEDGE_PROMPT),
        (Operator.hedge, "return", HEDGE_RETURN_PROMPT),
    ],
)
def test_rule_operators_use_the_prompt_for_their_boundary(op, boundary, prompt):
    # Breaks if a return reply is edited with the delegation prompt: on a return, what
    # carries the rule is the content that meets it, not an instruction.
    spy = Spy(["edited"])
    mutate = make_operator(op, "Note every risk.", spy, boundary=boundary)
    assert mutate("Some message.") == "edited"
    assert spy.prompts == [prompt.format(rule="Note every risk.", message="Some message.")]


@pytest.mark.parametrize(
    ("op", "words"), [(Operator.compress_100, 100), (Operator.compress_25, 25)]
)
def test_compress_summarises_then_cuts_to_the_word_cap(op, words):
    long_reply = "first line\n" + " ".join(f"w{i}" for i in range(300))
    spy = Spy([long_reply])
    out = make_operator(op, "rule", spy)("A long handoff message.")
    assert spy.prompts == [COMPRESS_PROMPT.format(words=words, message="A long handoff message.")]
    assert len(out.split()) == words
    assert out.startswith("first line\n")  # line breaks survive the cut


def test_compress_ignores_the_rule_and_applies_to_both_boundaries():
    # compress is a plain summary: the rule is not targeted, and the boundary does not matter.
    out = make_operator(Operator.compress_100, "rule", Spy(["short"]), boundary="return")
    assert out("anything") == "short"


@pytest.mark.parametrize(
    ("text", "n", "expected"),
    [
        ("a b c", 5, "a b c"),
        ("a b c d", 2, "a b"),
        ("a\nb  c\n\nd", 3, "a\nb  c"),
        ("  lead a b", 1, "  lead"),
        ("", 3, ""),
    ],
)
def test_first_words_keeps_original_spacing(text, n, expected):
    assert first_words(text, n) == expected


@pytest.mark.parametrize(
    "prompt", [DROP_PROMPT, DROP_RETURN_PROMPT, HEDGE_PROMPT, HEDGE_RETURN_PROMPT, COMPRESS_PROMPT]
)
def test_prompts_name_no_task_or_repo(prompt):
    # Generality (AGENTS.md rule 5): the prompts describe the method only.
    for word in ("migrations", "RISK", "planner", "coder", "reviewer"):
        assert word not in prompt


@pytest.mark.parametrize("op", [Operator.drop, Operator.hedge, Operator.compress_100])
def test_empty_reply_raises_instead_of_erasing_the_message(op):
    mutate = make_operator(op, "rule", Spy([""]))
    with pytest.raises(RuntimeError, match="empty"):
        mutate("message")


def test_unknown_boundary_kind_is_refused():
    with pytest.raises(ValueError, match="boundary"):
        make_operator(Operator.drop, "rule", Spy([]), boundary="compaction")


def test_return_corrupt_is_not_built():
    with pytest.raises(NotImplementedError):
        make_operator(Operator.return_corrupt, "rule", Spy([]))
