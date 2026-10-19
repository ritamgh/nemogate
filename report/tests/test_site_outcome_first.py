"""report/site.py: the page opens on what happened. Per rule, every arm's runs split into done,
kept the rule but left the task open, and broke the rule, with how many rule-breaking runs still
passed the task; block-only against gated after a block; the commit each rule's numbers are at.
Cells are built by hand, so every expected number below is checked by hand."""

import re

import pytest

from report import panels, site
from report.tests.test_site import (  # noqa: F401
    FIXTURES,
    load,
    make_cell,
    one_constraint,
    text_of,
    with_groups,
)
from report.tests.test_site_arm_metrics import BLOCKS, rate, stat, with_metrics
from schemas.ledger import load_ledger_yaml


@pytest.fixture
def rich() -> dict:
    """results_rich.json with C-002 graded, as test_site.py's fixture of the same name."""
    data = load("results_rich.json")
    data["constraints"]["C-002"]["has_check"] = True
    return data


def with_outcomes(cell: dict, done: int, kept_open: int, broke: int) -> dict:
    assert done + kept_open + broke == cell["n"] and broke == cell["violations"]
    cell["outcomes"] = {"done": done, "kept_open": kept_open, "broke": broke}
    return cell


def group(page: str, cid: str) -> str:
    """The rule's own group in the outcome section."""
    for block in re.findall(r'<section class="ogroup".*?</section>', page, flags=re.S):
        if f'data-rule="{cid}"' in block:
            return block
    raise AssertionError(cid)


def split_text(page: str) -> str:
    block = re.search(r'<p class="split-note">.*?</p>', page, flags=re.S)
    assert block, "no split sentence"
    return re.sub(r"\s+([,.:;)])", r"\1", text_of(block.group(0))).strip()


def page_with(cells: list[dict], task_ids=("TX",)) -> dict:
    data = one_constraint({"delegation:planner->coder": {"cells": cells, "comparisons": []}})
    data["constraints"]["C-900"]["task_ids"] = list(task_ids)
    return data


# --- the outcome split and the sentence that a broken rule can still pass ---------------------


def test_each_arm_row_shows_done_kept_open_and_broke_with_the_rule_breakers_that_passed():
    # Breaks if the first screen leaves out the three-way split, or counts "broke but passed"
    # as anything other than task successes minus the runs that were done (7 - 5 = 2 of 4).
    natural = with_outcomes(make_cell("natural", "none", 10, 4, 7), 5, 1, 4)
    gated = with_outcomes(make_cell("gated", "drop", 8, 0, 6), 6, 2, 0)
    text = text_of(group(site.render(page_with([natural, gated])), "C-900"))

    assert "Natural no sabotage 10 50% 5 of 10 runs 1 4 2 of 4 7 of 10" in text
    assert "Gated (NemoGate) rule lost (drop) 8 75% 6 of 8 runs 2 0 none broke it 6 of 8" in text


def test_the_headline_is_done_not_task_passed_and_broke_never_looks_like_success():
    # Breaks if the clean success (rule kept and task done) is read off `successes`, which also
    # counts runs that broke the rule (7 here, against 5 done), or if a rule-breaking run that
    # passed is drawn or styled as a success.
    natural = with_outcomes(make_cell("natural", "none", 10, 4, 7), 5, 1, 4)
    page = site.render(page_with([natural]))
    row = re.search(r"<tr>.*?</tr>", group(page, "C-900").split("<tbody>")[1], flags=re.S).group(0)

    headline = re.search(
        r'<td[^>]*data-label="Did the job without breaking the rule"[^>]*>(.*?)</td>',
        row,
        flags=re.S,
    ).group(1)
    assert text_of(headline).strip() == "50% 5 of 10 runs"
    passed = re.search(
        r'<td[^>]*data-label="Task passed \(rule not considered\)"[^>]*>(.*?)</td>', row, flags=re.S
    ).group(1)
    assert text_of(passed).strip() == "7 of 10"
    assert 'class="seg broke" style="width:40%"' in row
    assert 'class="seg done" style="width:50%"' in row
    assert 'class="seg open" style="width:10%"' in row
    broke_css = re.search(r"\.seg\.broke\{[^}]*\}", page).group(0)
    assert "--done" not in broke_css and "--good" not in broke_css
    assert "a run that broke the rule can still pass the task" in text_of(page)


def test_the_sentence_names_a_cell_whose_rule_breakers_still_passed_with_its_own_numbers():
    # Breaks if the page never says that task success and keeping the rule are separate, or if
    # its example is hard-coded instead of taken from the cell with the most such runs (the
    # natural cell, 2, over the mutated one, 1).
    natural = with_outcomes(make_cell("natural", "none", 10, 4, 7), 5, 1, 4)
    mutated = with_outcomes(make_cell("mutated", "drop", 10, 3, 4), 3, 4, 3)
    sentence = split_text(site.render(page_with([natural, mutated])))

    assert sentence == (
        "Task success and keeping the rule are counted separately, so a run can break the rule "
        "and still count as a task success. For example, C-900 (task TX), natural runs with no "
        "sabotage: 4 of 10 runs broke the rule and 2 of those 4 still passed the task, so task "
        "success reads 7 of 10 while 5 of 10 did the task and kept the rule."
    )


def test_no_example_is_given_when_no_rule_breaking_run_passed():
    # Breaks if the example sentence appears when the data cannot show it (every success here
    # kept the rule: successes equal done).
    natural = with_outcomes(make_cell("natural", "none", 10, 4, 5), 5, 1, 4)
    sentence = split_text(site.render(page_with([natural])))

    assert "For example" not in sentence
    assert sentence == (
        "Task success and keeping the rule are counted separately. In these results no run that "
        "broke the rule passed the task, so every task success here also kept the rule."
    )


def test_a_results_file_without_outcomes_says_it_cannot_tell(rich):
    # Breaks if an older results file (no `outcomes`) gets an invented split, or no sentence.
    page = site.render(rich)

    assert split_text(page) == (
        "Task success and keeping the rule are counted separately, so a run can break the rule "
        "and still count as a task success. This results file has no per-run outcome split, so "
        "this page cannot say how often that happened."
    )
    text = text_of(group(page, "C-001"))
    assert (
        "Mutated rule lost (drop) delegation:planner->coder 10 not in this results file "
        "– 5 – 5 of 10" in text
    )


# --- which commit each rule's numbers are at ------------------------------------------------


def test_each_rule_group_names_its_own_commit_and_the_header_lists_them_all(rich):
    # Breaks if the page shows one repo commit for runs pooled at several, or a rule's group
    # leaves out the commit its numbers are at.
    page = site.render(with_groups(rich, other_sha="4d81a3dbbb0000"))

    assert "f06137eaf4a6" in group(page, "C-001") and "4d81a3dbbb00" not in group(page, "C-001")
    assert "4d81a3dbbb00" in group(page, "C-002")
    assert (
        "This page pools runs made at 2 repo commits; each rule's numbers are at its own commit, "
        "named in its section: f06137eaf4a6 (C-001), 4d81a3dbbb00 (C-002)."
    ) in text_of(page)


def test_one_commit_for_every_rule_is_not_called_several(rich):
    # Breaks if the header claims several commits when all rules were pooled at one.
    assert "This page pools runs made at" not in text_of(site.render(with_groups(rich)))


# --- block-only against gated after a block --------------------------------------------------


GATED_BLOCKS = {
    **BLOCKS,
    "n_traced": 4,
    "runs_with_block": 0,
    "blocked": stat(0.0, 0.0, 0.0, 4),
    "repeats": stat(0.0, 0.0, 0.0, 4),
    "stuck": None,
    "recovery": None,
}


def blocks_page() -> dict:
    block_only = with_metrics(
        make_cell("block_only", "drop", 2, 0, 1),
        attempt_rate=rate(1, 2, 0.5, 0.0945, 0.9055),
        blocks=BLOCKS,
    )
    gated = with_metrics(
        make_cell("gated", "drop", 4, 0, 4),
        attempt_rate=rate(0, 4, 0.0, 0.0, 0.4899),
        blocks=GATED_BLOCKS,
    )
    comp = {
        "name": "attempt_reduction",
        "operator": "drop",
        "a": {"arm": "gated", "operator": "drop"},
        "b": {"arm": "block_only", "operator": "drop"},
        "n_a": 4,
        "n_b": 2,
        "diff": -0.5,
        "lo": -0.9,
        "hi": 0.1,
    }
    data = page_with([block_only, gated])
    data["constraints"]["C-900"]["boundaries"]["delegation:planner->coder"]["comparisons"] = [comp]
    return data


def blocks_table(page: str) -> str:
    block = re.search(r'<table class="blocks".*?</table>', page, flags=re.S)
    assert block, "no block-only against gated table"
    return block.group(0)


def test_the_block_table_puts_block_only_and_gated_side_by_side_with_their_numbers():
    # Breaks if the blocked calls, retries, stuck runs and attempts per arm stay in the small
    # print instead of one table where block-only and gated are compared.
    page = site.render(blocks_page())
    text = text_of(blocks_table(page))

    assert (
        "Block-only rule lost (drop) 1 of 2 1.0 (range 0.0 to 2.0) 0.5 (range 0.0 to 1.0) "
        "1 of 1 1 of 2 (50%)"
    ) in text
    assert (
        "Gated (NemoGate) rule lost (drop) 0 of 4 0.0 (range 0.0 to 0.0) 0.0 (range 0.0 to 0.0) "
        "no run was blocked 0 of 4 (0%)"
    ) in text
    assert (
        "Gated minus block-only, runs that tried the forbidden action (drop): -50 pp (95% "
        "interval -90 pp to +10 pp), not distinguishable from 0 at n = 4 and 2."
    ) in text_of(group(page, "C-900"))


def test_no_block_table_without_both_blocking_arms_or_without_traces(rich):
    # Breaks if a block table is printed from cells that have no block counts (an older file).
    assert '<table class="blocks"' not in site.render(rich)


# --- order of the page, and what must not change -------------------------------------------


def test_the_results_come_first_and_the_key_and_method_text_is_still_there(rich):
    # Breaks if the key and "What each part fixes" stay above the results, or are dropped.
    page = site.render(rich)
    text = text_of(page)

    assert page.index('id="outcomes-h"') < page.index('id="key-h"')
    assert page.index('id="outcomes-h"') < page.index('id="fixes-h"')
    for _, line in site.FIXES:
        assert line in text
    assert "is the hypothesis being measured, not a result." in text
    assert "Every rate comes with a 95% Wilson score interval." in text


def test_how_to_read_this_box_is_at_most_four_lines_and_terms_are_defined(rich):
    # Breaks if the reading guide grows into the wall of method text it replaces, or a term the
    # page uses goes undefined.
    page = site.render(rich)
    box = re.search(r'<aside class="howto".*?</aside>', page, flags=re.S).group(0)
    assert box.count("<li>") <= 4
    terms = text_of(re.search(r'<dl class="kv stack terms">.*?</dl>', page, flags=re.S).group(0))
    for term in (
        "Boundary",
        "Inconclusive",
        "Not comparable",
        "95% interval",
        "Compaction",
    ):
        assert term in terms


def test_an_old_format_results_file_without_optional_fields_renders(tmp_path):
    # Breaks if the outcome section reads a field older files do not have (task_ids, outcomes,
    # arm_metrics, labels, per-rule reproducibility, blocked_before_call).
    data = load("results_empty.json")
    data["constraints"] = {
        "C-900": {
            "text": "A rule.",
            "has_check": True,
            "boundaries": {"delegation:a->b": {"cells": [make_cell("natural", "none", 3, 1, 2)]}},
        }
    }
    page = site.render(data)

    assert "Natural no sabotage 3 not in this results file – 1 – 2 of 3" in text_of(
        group(page, "C-900")
    )


def test_the_page_has_no_script_and_no_external_url(rich):
    # Breaks if anything makes the page load code or reach the network.
    for page in (
        site.render(rich),
        site.render(blocks_page()),
        site.render(load("results_runs.json")),
    ):
        assert "<script" not in page.lower()
        assert not re.search(r"(?:https?:)?//[a-z0-9]", page, flags=re.I)


def test_identical_input_renders_identical_bytes(rich):
    # Breaks if the new sections depend on anything but the input (order of a set, a clock).
    assert site.render(blocks_page()) == site.render(blocks_page())


# --- a rule that was never exercised -----------------------------------------------------------


def test_a_never_exercised_rule_says_whether_the_run_folders_held_any_of_its_runs(tmp_path):
    # Breaks if a never-exercised rule reads as plain "never exercised" when the run folders say
    # why: here C-001 has 4 runs (counted) and the other rules none.
    results = load("results_runs.json")
    ledger = load_ledger_yaml((FIXTURES / "ledger.yaml").read_text(encoding="utf-8"))
    trace = panels.load_trace_evidence(
        [FIXTURES / "run_folders"], results["reproducibility"], site.rule_groups(results)
    )
    page = site.render(results, trace=trace, ledger=ledger)
    never = re.search(r"Never exercised.*?</ul>", page, flags=re.S).group(0)

    unexercised = [c.id for c in ledger.constraints if c.id not in results["constraints"]]
    assert unexercised
    for cid in unexercised:
        assert f"<code>{cid}</code>" in never
    assert text_of(never).count("No run of this rule is in the run folders this page read.") == (
        len(unexercised)
    )


def test_without_run_folders_no_reason_is_given(rich):
    # Breaks if a reason is made up when the page has no run folders to read it from.
    ledger = load_ledger_yaml((FIXTURES / "ledger.yaml").read_text(encoding="utf-8"))
    page = site.render(load("results_runs.json"), ledger=ledger)

    assert "run folders this page read" not in page
