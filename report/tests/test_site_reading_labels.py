"""report/site.py: how the outcome tables read to someone outside the project. The unfinished
state is "Rule kept, task not finished" (it is used for natural runs too, where nothing was
blocked), a task pass that broke the rule is named as such under the bar, and the reading guide
says an unfinished task with the rule kept is not a gate failure. Cells are built by hand, so
every expected number is checked by hand."""

import re

import pytest

from report import site
from report.tests.test_site import make_cell, one_constraint, text_of
from report.tests.test_site_appendix import appendix_part, build
from report.tests.test_site_arm_metrics import BLOCKS, rate, with_metrics
from report.tests.test_site_outcome_first import group, page_with, with_outcomes

LABEL = "Rule kept, task not finished"
OLD = ("left the task open", "left open", "left it open")
READING = (
    "The gate stops the rule from being broken. Finishing the task after a block needs a route "
    "that keeps the rule or a human who can approve; that depends on the agent system around "
    "the gate, so an unfinished task with the rule kept is not a gate failure."
)


def blocks(runs_with_block: int, stuck: tuple[int, int] | None) -> dict:
    return {
        **BLOCKS,
        "runs_with_block": runs_with_block,
        "stuck": None if stuck is None else rate(stuck[0], stuck[1], stuck[0] / stuck[1], 0, 1),
    }


def rows(page: str, cid: str = "C-900") -> list[str]:
    """The outcome table's rows of a rule, as text, in order."""
    table = re.search(r'<table class="outcome".*?</table>', group(page, cid), flags=re.S).group(0)
    body = table.split("<tbody>")[1]
    return [text_of(r) for r in re.findall(r"<tr>.*?</tr>", body, flags=re.S)]


def mixed_cells() -> list[dict]:
    natural = with_outcomes(make_cell("natural", "none", 10, 4, 7), 5, 1, 4)
    block_only = with_metrics(
        with_outcomes(make_cell("block_only", "drop", 5, 0, 0), 0, 5, 0), blocks=blocks(5, (4, 5))
    )
    return [natural, block_only]


def appendix_of(tmp_path, cells: list[dict]) -> str:
    data = page_with(cells)
    return appendix_part(build(tmp_path, [("Second set", data, None)]))


# --- the label --------------------------------------------------------------------------------


def test_the_unfinished_state_is_named_in_the_key_table_and_card_and_the_old_wording_is_gone():
    # Breaks if any place still says the rule was "left open" (it reads as a failure, and is false
    # for natural runs): the key, the table header and cell label, the card's split and its foot
    # note, the reading guide and the page's own docstring wording.
    page = site.render(page_with(mixed_cells()))
    text = text_of(page)

    assert f'<li><span class="seg open"></span>{LABEL}</li>' in page
    assert f'<th scope="col">{LABEL}</th>' in page
    assert f'data-label="{LABEL}"' in page
    assert "done 5 · rule kept, task not finished 1 · broke the rule 4" in text
    assert f"{LABEL}: the rule was obeyed but the task was not finished" in text
    for old in OLD:
        assert old not in text, old
        assert old not in page, old


def test_the_old_wording_is_gone_from_an_appendix_and_the_new_label_is_there(tmp_path):
    # Breaks if the appendix draws its key or table from a copy of the old labels.
    part = appendix_of(tmp_path, mixed_cells())

    assert f'<th scope="col">{LABEL}</th>' in part
    assert f'<li><span class="seg open"></span>{LABEL}</li>' in part
    for old in OLD:
        assert old not in part, old


def test_the_old_wording_is_gone_from_a_page_without_outcomes():
    # Breaks if the key or reading guide keep the old words when no cell carries `outcomes`.
    page = site.render(one_constraint({"b": {"cells": [make_cell("natural", "none", 4, 1, 2)]}}))

    assert LABEL in page
    for old in OLD:
        assert old not in page, old


# --- the sub-line about blocks ----------------------------------------------------------------


def test_the_block_line_counts_unfinished_runs_that_were_stopped_by_a_block():
    # Breaks if the line is missing for an arm with blocks, uses a number the data does not
    # carry, or appears on the natural row, where nothing was blocked. Block-only: 5 runs with a
    # block, 4 of them failed the task, none broke the rule, so all 4 of the 5 unfinished runs
    # were stopped by a block.
    natural, block_only = rows(site.render(page_with(mixed_cells())))

    assert "4 of 5 were stopped by a block" in block_only
    assert "stopped by a block" not in natural


def test_the_block_line_gives_a_range_when_a_rule_breaker_could_be_among_the_stuck():
    # Breaks if a stuck run that broke the rule (not unfinished-with-the-rule-kept) is counted
    # as stopped. 5 runs, 3 unfinished, 2 broke it; 4 had a block and failed the task. At least
    # 4 - 2 = 2 of those are among the 3 unfinished; at most 3.
    cell = with_metrics(
        with_outcomes(make_cell("gated", "drop", 5, 2, 0), 0, 3, 2), blocks=blocks(5, (4, 5))
    )
    (row,) = rows(site.render(page_with([cell])))

    assert "2 to 3 of 3 were stopped by a block" in row


@pytest.mark.parametrize(
    "metrics",
    [
        {"blocks": None},  # no trace to count from
        {"blocks": blocks(0, None)},  # traced, nobody was blocked
        {"blocks": blocks(2, (0, 2))},  # blocked, but none of them failed the task
    ],
    ids=["no-trace", "no-block", "none-stuck"],
)
def test_the_block_line_is_absent_when_the_data_shows_no_stopped_run(metrics):
    # Breaks if the line is drawn from a guess: no trace, no block, or no block-then-fail run.
    cell = with_metrics(with_outcomes(make_cell("gated", "drop", 5, 0, 0), 0, 5, 0), **metrics)
    (row,) = rows(site.render(page_with([cell])))

    assert "stopped by a block" not in row


def test_the_block_line_is_absent_for_a_cell_without_arm_metrics():
    # Breaks if a results file from before arm_metrics gets an invented line.
    cell = with_outcomes(make_cell("gated", "drop", 5, 0, 0), 0, 5, 0)
    (row,) = rows(site.render(page_with([cell])))

    assert "stopped by a block" not in row


# --- the task passes that broke the rule ------------------------------------------------------


def test_a_task_pass_that_broke_the_rule_is_named_under_the_bar_with_successes_minus_done():
    # Breaks if the line shows another number than successes - done (7 - 5 = 2 of 10), or sits
    # anywhere but under the outcome bar in the headline cell.
    natural = with_outcomes(make_cell("natural", "none", 10, 4, 7), 5, 1, 4)
    page = site.render(page_with([natural]))
    headline = re.search(r'<td[^>]*class="headline"[^>]*>(.*?)</td>', page, flags=re.S).group(1)

    assert text_of(headline).strip() == (
        "50% 5 of 10 runs Passed the task but broke the rule: 2 of 10 runs"
    )
    assert headline.index('class="obar"') < headline.index("Passed the task but broke the rule")
    assert "7 of 10" in rows(page)[0]  # the plain task-passed figure stays where it was


@pytest.mark.parametrize(
    ("successes", "done"), [(5, 5), (0, 0)], ids=["all-passes-kept-the-rule", "no-passes"]
)
def test_the_passed_but_broke_line_is_absent_when_no_pass_broke_the_rule(successes, done):
    # Breaks if the line is shown with 0 (successes equal done: every pass kept the rule).
    cell = with_outcomes(make_cell("natural", "none", 10, 4, successes), done, 6 - done, 4)
    (row,) = rows(site.render(page_with([cell])))

    assert "Passed the task but broke the rule" not in row


def test_the_passed_but_broke_line_is_absent_without_outcomes():
    # Breaks if a results file from before `outcomes` gets a number made up from `successes`.
    (row,) = rows(site.render(page_with([make_cell("natural", "none", 10, 4, 7)])))

    assert "Passed the task but broke the rule" not in row
    assert "not in this results file" in row


def test_the_passed_but_broke_line_is_in_an_appendix_too(tmp_path):
    # Breaks if the appendix draws its rows from another renderer than the main page.
    part = appendix_of(tmp_path, mixed_cells())

    assert "Passed the task but broke the rule: 2 of 10 runs" in text_of(part)
    assert "4 of 5 were stopped by a block" in text_of(part)


# --- the reading guide and the page's bytes ---------------------------------------------------


def test_the_reading_guide_says_an_unfinished_task_with_the_rule_kept_is_not_a_gate_failure():
    # Breaks if the guide drops the sentences, or keeps them out of the "How to read this" box.
    page = site.render(page_with(mixed_cells()))
    howto = re.search(r'<aside class="howto".*?</aside>', page, flags=re.S).group(0)

    assert READING in text_of(howto)


def test_the_page_stays_static_and_byte_identical_for_the_same_input(tmp_path):
    # Breaks if the new markup brings a script, a link out, or anything that varies between runs.
    one = build(tmp_path, [("Set", page_with(mixed_cells()), None)], name="one.html")
    two = build(tmp_path, [("Set", page_with(mixed_cells()), None)], name="two.html")

    assert one == two
    assert "<script" not in one
    assert "http" not in one


def test_no_number_changes_only_the_label_and_the_added_lines():
    # Breaks if the relabel touches a count: the rows keep their runs, bar widths and the
    # figures of the existing columns (natural: 5 done, 1 not finished, 4 broke, 2 of 4, 7 of 10).
    page = site.render(page_with(mixed_cells()))
    natural = rows(page)[0]

    assert natural.strip().endswith("1 4 2 of 4 7 of 10")
    assert 'class="seg done" style="width:50%"' in page
    assert 'class="seg open" style="width:10%"' in page
    assert 'class="seg broke" style="width:40%"' in page
