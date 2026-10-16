"""report/site.py: the per-arm cost, time and blocking table, errored-run cost, and the wording
that credits blocking (not NemoGate) for "broken 0 of N". Cells are built by hand."""

import re

from report import site
from report.tests.test_site import make_cell, one_constraint, rich, text_of  # noqa: F401


def stat(mean, lo, hi, n):
    return {"n": n, "mean": mean, "lo": lo, "hi": hi}


def rate(k, n, r, lo, hi):
    return {"k": k, "n": n, "rate": r, "lo": lo, "hi": hi}


def with_metrics(cell: dict, **metrics) -> dict:
    cell["arm_metrics"] = {
        "cost": stat(0.1099, 0.0672, 0.1525, 2),
        "wall_seconds": {**stat(None, None, None, 0), "unknown": 2},
        "attempt_rate": None,
        "blocks": None,
        **metrics,
    }
    return cell


BLOCKS = {
    "n_traced": 2,
    "runs_with_block": 1,
    "blocked": stat(1.0, 0.0, 2.0, 2),
    "repeats": stat(0.5, 0.0, 1.0, 2),
    "carry_over": stat(0.0, 0.0, 0.0, 2),
    "replan": rate(1, 3, 0.3333, 0.0615, 0.7923),
    "stuck": rate(1, 1, 1.0, 0.2065, 1.0),
    "recovery": {
        "n": 1,
        "steps": stat(4.0, None, None, 1),
        "cost_usd_est": stat(0.1667, None, None, 1),
        "seconds_est": stat(66.7, None, None, 1),
    },
}


def metrics_page() -> dict:
    cells = [
        with_metrics(make_cell("mutated", "drop", 10, 5, 5)),
        with_metrics(
            make_cell("block_only", "drop", 2, 0, 1),
            wall_seconds={**stat(150.0, 100.0, 200.0, 2), "unknown": 0},
            attempt_rate=rate(1, 2, 0.5, 0.0945, 0.9055),
            blocks=BLOCKS,
        ),
        make_cell("gated", "drop", 3, 0, 3),  # a results file from before arm_metrics
    ]
    return one_constraint({"delegation:a->b": {"cells": cells, "comparisons": []}})


def metrics_table(page: str) -> str:
    return re.search(r'<table class="cells arms".*?</table>', page, flags=re.S).group(0)


def test_cost_time_and_blocking_are_shown_per_arm_with_their_intervals():
    # Breaks if the per-arm numbers in results.json never reach the page.
    text = text_of(metrics_table(site.render(metrics_page())))

    assert "Block-only drop $0.110 ($0.067 to $0.153) mean of 2 runs" in text
    assert "150 s (100 to 200 s) mean of 2 runs" in text
    assert "50% (9–91%) 1 of 2 runs, blocked or not" in text
    assert "1.0 blocked calls per run (0.0 to 2.0)" in text
    assert "0.5 repeated after a block per run (0.0 to 1.0)" in text
    assert "re-planned the same action in 1 of 3 next delegations" in text
    assert "stuck: 1 of 1 blocked runs failed the task" in text
    assert "recovery: 4 steps after the first block" in text


def test_unknown_time_and_missing_traces_are_said_not_shown_as_zero():
    text = text_of(metrics_table(site.render(metrics_page())))

    # the mutated cell: 2 runs with no timing and no trace
    assert "unknown no timing recorded" in text
    assert "no trace to count from" in text


def test_cost_and_time_of_each_compaction_setting_get_their_own_row():
    # Breaks if the cost table finds a cell by (arm, operator) alone: the compacted runs
    # ($0.250) would be hidden behind the uncompacted ones ($0.100).
    off = with_metrics(
        {**make_cell("natural", "none", 4, 0, 4), "compaction": {"tokens": None, "keep": None}},
        cost=stat(0.1, 0.08, 0.12, 4),
    )
    on = with_metrics(
        {**make_cell("natural", "none", 4, 2, 2), "compaction": {"tokens": "6000", "keep": "6"}},
        cost=stat(0.25, 0.2, 0.3, 4),
    )
    page = one_constraint({"delegation:a->b": {"cells": [off, on], "comparisons": []}})

    text = text_of(metrics_table(site.render(page)))

    assert "none compaction off $0.100 ($0.080 to $0.120) mean of 4 runs" in text
    assert "none compaction at 6000 tokens, keep 6 $0.250 ($0.200 to $0.300) mean of 4 runs" in text


def test_arms_without_a_gate_do_not_get_block_counts():
    # Breaks if a mutated or natural row prints "0.0 blocked calls per run", as if a gate had
    # looked and found nothing.
    page = metrics_page()
    page["constraints"]["C-900"]["boundaries"]["delegation:a->b"]["cells"][0]["arm_metrics"][
        "blocks"
    ] = BLOCKS
    text = text_of(metrics_table(site.render(page)))

    assert "Mutated drop" in text
    assert text.count("blocked calls per run") == 1  # block-only only
    assert "no gate in this arm, nothing is blocked" in text


def test_a_cell_from_an_older_results_file_is_shown_without_the_new_numbers():
    # Breaks if a results file with no arm_metrics crashes the page or prints made-up zeros.
    text = text_of(metrics_table(site.render(metrics_page())))

    assert "Gated (NemoGate) drop not in this results file" in text


def test_a_page_with_no_arm_metrics_at_all_has_no_per_arm_table():
    cells = [make_cell("gated", "drop", 3, 0, 3)]
    page = site.render(one_constraint({"delegation:a->b": {"cells": cells, "comparisons": []}}))

    assert "cells arms" not in page


def test_errored_run_cost_is_listed_with_the_run_counts(rich):  # noqa: F811
    rich["runs"]["errored_cost"] = {
        "count": 2,
        "cost_usd": 0.1101,
        "cost_unknown": 1,
        "by_arm": {"block_only": {"count": 2, "cost_usd": 0.1101, "cost_unknown": 1}},
    }

    text = text_of(site.render(rich))

    assert "spent on errored runs $0.110 (2 runs, 1 with no recorded cost)" in text


def test_the_gate_note_says_a_zero_comes_from_blocking_and_gated_is_a_hypothesis(rich):  # noqa: F811
    # Breaks if the page credits NemoGate for what blocking alone produces: the block-only arm
    # refuses with the oracle's own check, so it gets 0 broken by construction.
    text = text_of(site.render(rich))
    section = text.split("What each part fixes", 1)[1].split("What these numbers can")[0]

    assert "A 0 broken in the block-only and gated arms comes from blocking" in section
    assert "by construction" in section
    assert "Whether carrying the rule adds anything over blocking" in section
    assert "is the hypothesis being measured, not a result" in section
    assert "not a result" in section
