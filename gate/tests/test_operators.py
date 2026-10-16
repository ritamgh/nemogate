import pytest
from langchain_core.messages import HumanMessage

from common.llm import get_model
from gate import NothingToMutate
from gate.operators import (
    COMPRESS_PROMPT,
    HEDGE_PROMPT,
    HEDGE_RETURN_PROMPT,
    SELECT_PROMPT,
    SELECT_RETURN_PROMPT,
    first_words,
    make_operator,
    numbered,
    sentences,
)
from gate.tests.stubs import picks, rewrites
from schemas.enums import Operator


class Spy:
    """Wraps a stub model and records each prompt, plain or structured."""

    def __init__(self, replies):
        self.model = get_model("helper", stub_responses=replies)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.model.invoke([HumanMessage(prompt)])

    def with_structured_output(self, schema):
        bound, spy = self.model.with_structured_output(schema), self

        class _Recorder:
            def invoke(self, prompt):
                spy.prompts.append(prompt)
                return bound.invoke(prompt)

        return _Recorder()


DELEGATION = "Build the export. Never touch vendor/. Add a test."
REPLY = "Looks fine.\nRISK: old clients get 20 by default.\nThanks."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (DELEGATION, ["Build the export.", "Never touch vendor/.", "Add a test."]),
        (REPLY, ["Looks fine.", "RISK: old clients get 20 by default.", "Thanks."]),
        ("One line, no stop", ["One line, no stop"]),
        ("A. B!\n\nC? D", ["A.", "B!", "C?", "D"]),
        ("Keep it, e.g. no X. Done.", ["Keep it, e.g. no X.", "Done."]),
        ("先建好。不要碰 vendor/。", ["先建好。", "不要碰 vendor/。"]),  # CJK full stops
        ("Never touch\nvendor/.", ["Never touch", "vendor/."]),  # a wrapped rule: two lines
        # Known limit: an abbreviation other than e.g. / i.e. ends a sentence.
        ("Ask Dr. Who first.", ["Ask Dr.", "Who first."]),
        ("", []),
    ],
)
def test_sentences_split_on_stops_and_line_breaks(text, expected):
    assert [text[s:e] for s, e in sentences(text)] == expected


NUMBERED = "Steps:\n1. Build.\n2. Never touch vendor/.\n3) Add a test."
BULLETED = "Steps:\n- Build.\n- Never touch vendor/.\n* Add a test."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (NUMBERED, ["Steps:", "1. Build.", "2. Never touch vendor/.", "3) Add a test."]),
        (BULLETED, ["Steps:", "- Build.", "- Never touch vendor/.", "* Add a test."]),
        ("Do it in 2. Then stop.", ["Do it in 2.", "Then stop."]),  # a number ending a sentence
        ("Do: 1. Add X. 2. Add Y.", ["Do: 1.", "Add X.", "2. Add Y."]),  # an inline list
    ],
    ids=["numbered", "bulleted", "mid-line-number", "inline-list"],
)
def test_a_list_marker_stays_with_its_item(text, expected):
    assert [text[s:e] for s, e in sentences(text)] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (NUMBERED, "Steps:\n1. Build.\n3) Add a test."),
        (BULLETED, "Steps:\n- Build.\n* Add a test."),
    ],
    ids=["numbered", "bulleted"],
)
def test_drop_removes_a_list_item_with_its_marker(text, expected):
    assert make_operator(Operator.drop, "rule", Spy([picks(3)]))(text) == expected


def test_drop_removes_a_rule_wrapped_over_two_lines():
    drop = make_operator(Operator.drop, "rule", Spy([picks(2, 3)]))
    assert drop("Build it.\nNever touch\nvendor/.\nAdd a test.") == "Build it.\nAdd a test."


def test_numbered_lists_sentences_from_one():
    assert numbered(DELEGATION) == "1. Build the export.\n2. Never touch vendor/.\n3. Add a test."


def test_none_operator_is_no_mutation():
    assert make_operator(Operator.none, "rule", Spy([])) is None


def test_drop_removes_the_named_sentences_and_nothing_else():
    # Live probes 2026-10-15: a free-form rewrite came back unchanged, and free quoting
    # over-selected; picking numbered sentences was exact (12/12), so drop asks only for the
    # numbers and removes those sentences itself.
    spy = Spy([picks(2)])
    drop = make_operator(Operator.drop, "Keep out of vendor/", spy)
    assert drop(DELEGATION) == "Build the export. Add a test."
    assert spy.prompts == [
        SELECT_PROMPT.format(rule="Keep out of vendor/", sentences=numbered(DELEGATION))
    ]


def test_drop_on_a_return_removes_the_line_that_meets_the_rule():
    spy = Spy([picks(2)])
    drop = make_operator(Operator.drop, "Note every risk.", spy, boundary="return")
    assert drop(REPLY) == "Looks fine.\nThanks."
    assert spy.prompts == [
        SELECT_RETURN_PROMPT.format(rule="Note every risk.", sentences=numbered(REPLY))
    ]


@pytest.mark.parametrize(
    ("chosen", "expected"),
    [
        ((2, 2), "Build the export. Add a test."),  # a repeat counts once
        ((1, 3), "Never touch vendor/."),
        ((9, 2, 0), "Build the export. Add a test."),  # out of range: ignored
    ],
    ids=["repeat", "several", "some-out-of-range"],
)
def test_drop_handles_any_set_of_numbers(chosen, expected):
    assert make_operator(Operator.drop, "rule", Spy([picks(*chosen)]))(DELEGATION) == expected


@pytest.mark.parametrize(
    "reply",
    [
        picks(),  # nothing concerns the rule
        picks(9, 0, -1),  # every number out of range
        rewrites(),  # hedge: no rewrites
        rewrites((7, "x")),  # hedge: out of range
        rewrites((2, "Never touch vendor/.")),  # hedge: the same sentence back
    ],
    ids=["no-picks", "picks-out-of-range", "no-rewrites", "rewrite-out-of-range", "same-text"],
)
def test_a_mutation_that_changes_nothing_raises_nothing_to_mutate(reply):
    # A run whose handoff was not mutated must not be recorded as mutated; replay catches the
    # type and counts the run as a non-sample.
    op = Operator.drop if reply.tool_calls[0]["name"] == "Picks" else Operator.hedge
    with pytest.raises(NothingToMutate):
        make_operator(op, "rule", Spy([reply]))(DELEGATION)


@pytest.mark.parametrize("op", [Operator.drop, Operator.hedge])
def test_a_reply_that_does_not_parse_raises(op):
    with pytest.raises(RuntimeError, match="did not parse"):
        make_operator(op, "rule", Spy(["plain text, no tool call"]))(DELEGATION)


@pytest.mark.parametrize(
    ("op", "reply"),
    [
        (Operator.drop, picks(1, 2, 3)),
        (Operator.hedge, rewrites((1, ""), (2, " "), (3, ""))),
    ],
    ids=["drop", "hedge-all-empty"],
)
def test_a_mutation_that_would_erase_everything_raises(op, reply):
    with pytest.raises(RuntimeError, match="empty") as caught:
        make_operator(op, "rule", Spy([reply]))(DELEGATION)
    assert not isinstance(caught.value, NothingToMutate)


@pytest.mark.parametrize("boundary", ["delegation", "return"])
def test_drop_on_a_one_sentence_message_raises(boundary):
    # Nothing sensible is left once its only sentence goes, so the run cannot be a sample.
    drop = make_operator(Operator.drop, "rule", Spy([picks(1)]), boundary=boundary)
    with pytest.raises(RuntimeError, match="empty"):
        drop("Never touch vendor/.")


@pytest.mark.parametrize(
    ("boundary", "prompt", "text", "pair", "expected"),
    [
        (
            "delegation",
            HEDGE_PROMPT,
            DELEGATION,
            (2, "Use your judgement about vendor/."),
            "Build the export. Use your judgement about vendor/. Add a test.",
        ),
        (
            "return",
            HEDGE_RETURN_PROMPT,
            REPLY,
            (2, "Some clients may notice changes."),
            "Looks fine.\nSome clients may notice changes.\nThanks.",
        ),
    ],
    ids=["delegation", "return"],
)
def test_hedge_swaps_in_the_helpers_vague_version(boundary, prompt, text, pair, expected):
    spy = Spy([rewrites(pair)])
    hedge = make_operator(Operator.hedge, "a rule", spy, boundary=boundary)
    assert hedge(text) == expected
    assert spy.prompts == [prompt.format(rule="a rule", sentences=numbered(text))]


def test_hedge_with_an_empty_replacement_removes_the_sentence_cleanly():
    hedge = make_operator(Operator.hedge, "rule", Spy([rewrites((2, "  "))]))
    assert hedge(DELEGATION) == "Build the export. Add a test."


def test_hedge_ignores_numbers_out_of_range():
    hedge = make_operator(Operator.hedge, "rule", Spy([rewrites((7, "x"), (2, "Maybe."))]))
    assert hedge(DELEGATION) == "Build the export. Maybe. Add a test."


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


@pytest.mark.parametrize("boundary", ["delegation", "return"])
def test_compress_ignores_the_rule_and_applies_to_both_boundaries(boundary):
    # compress is a plain summary: the rule is not targeted, and the boundary does not matter.
    out = make_operator(Operator.compress_100, "rule", Spy(["short"]), boundary=boundary)
    assert out("anything") == "short"


def test_compress_with_an_empty_summary_raises():
    with pytest.raises(RuntimeError, match="empty"):
        make_operator(Operator.compress_100, "rule", Spy([""]))("message")


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
    "prompt",
    [SELECT_PROMPT, SELECT_RETURN_PROMPT, HEDGE_PROMPT, HEDGE_RETURN_PROMPT, COMPRESS_PROMPT],
)
def test_prompts_name_no_task_or_repo(prompt):
    # Generality (AGENTS.md rule 5): the prompts describe the method only.
    for word in ("migrations", "RISK", "planner", "coder", "reviewer"):
        assert word not in prompt


def test_unknown_boundary_kind_is_refused():
    with pytest.raises(ValueError, match="boundary"):
        make_operator(Operator.drop, "rule", Spy([]), boundary="compaction")


def test_return_corrupt_is_not_built():
    with pytest.raises(NotImplementedError):
        make_operator(Operator.return_corrupt, "rule", Spy([]))
