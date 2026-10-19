"""report/site.py: the per-arm cost, time and blocking table, errored-run cost, and the wording
that credits blocking (not NemoGate) for "broken 0 of N". Cells are built by hand."""

import re

from report import site
from report.tests.test_site import (  # noqa: F401
    FORMAT_V1_DELEGATION,
    make_cell,
    one_constraint,
    raise_c002_effect,
    rich,
    text_of,
    verdict_text,
)


def stat(mean, lo, hi, n, basis="range"):
    return {"n": n, "mean": mean, "lo": lo, "hi": hi, "basis": None if lo is None else basis}


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

    assert "Block-only drop $0.110 (range $0.067 to $0.153) mean of 2 runs" in text
    assert "150 s (range 100 to 200 s) mean of 2 runs" in text
    assert "1.0 blocked calls per run (range 0.0 to 2.0)" in text
    assert "0.5 repeated after a block per run (range 0.0 to 1.0)" in text
    assert "re-planned the same action in 1 of 3 next delegations" in text
    assert "stuck: 1 of 1 blocked runs failed the task" in text
    assert "recovery: 4 steps after the first block" in text


def test_unknown_time_and_missing_traces_are_said_not_shown_as_zero():
    text = text_of(metrics_table(site.render(metrics_page())))

    # the mutated cell: 2 runs with no timing and no trace
    assert "unknown no timing recorded" in text


def test_cost_and_time_of_each_compaction_setting_get_their_own_row():
    # Breaks if the cost table finds a cell by (arm, operator) alone: the compacted runs
    # ($0.250) would be hidden behind the uncompacted ones ($0.100).
    off = with_metrics(
        {**make_cell("natural", "none", 4, 0, 4), "compaction": {"tokens": None, "keep": None}},
        cost=stat(0.1, 0.08, 0.12, 4),
    )
    on = with_metrics(
        {**make_cell("natural", "none", 4, 2, 2), "compaction": {"tokens": 6000, "keep": 6}},
        cost=stat(0.25, 0.2, 0.3, 4),
    )
    page = one_constraint({"delegation:a->b": {"cells": [off, on], "comparisons": []}})

    text = text_of(metrics_table(site.render(page)))

    assert "none compaction off $0.100 (range $0.080 to $0.120) mean of 4 runs" in text
    assert (
        "none compaction at 6000 tokens, keep 6 $0.250 (range $0.200 to $0.300) mean of 4 runs"
        in text
    )


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

    assert "For a path, tool or approval rule, a 0 broken in the block-only and gated" in section
    assert "comes from blocking" in section
    assert "by construction" in section
    assert "a 0 there is a measured result" in section  # other rules are not blocked ahead
    assert "Whether carrying the rule adds anything over blocking" in section
    assert "is the hypothesis being measured, not a result" in section
    assert "not a result" in section


# --- a 0 is credited to blocking only where blocking can have produced it ------------------------


def verdict_page(rich, *, blocked_before_call, gated_blocks=None):  # noqa: F811
    raise_c002_effect(rich)
    body = rich["constraints"]["C-002"]
    body["blocked_before_call"] = blocked_before_call
    if gated_blocks is not None:
        cells = body["boundaries"][FORMAT_V1_DELEGATION]["cells"]
        gated = next(c for c in cells if c["arm"] == "gated" and c["operator"] == "drop")
        with_metrics(gated, blocks={**BLOCKS, "runs_with_block": gated_blocks})
    return verdict_text(site.render(rich))


def test_no_blocking_credit_for_a_rule_the_gate_does_not_block_before_the_call(rich):  # noqa: F811
    # Breaks if a re-pin's 0 on a frozen-file or output rule is explained away as blocking.
    text = verdict_page(rich, blocked_before_call=False)

    assert "0 of 10 gated runs" in text
    assert "comes from blocking" not in text


def test_no_blocking_credit_when_no_gated_run_was_blocked(rich):  # noqa: F811
    # The gated runs never tried the action (0 runs with a block), so blocking did not make
    # the 0.
    text = verdict_page(rich, blocked_before_call=True, gated_blocks=0)

    assert "0 of 10 gated runs" in text
    assert "comes from blocking" not in text


def test_blocking_credit_when_a_gated_run_was_blocked_on_a_rule_the_gate_blocks(rich):  # noqa: F811
    text = verdict_page(rich, blocked_before_call=True, gated_blocks=2)

    assert text.endswith("not show that carrying the rule helps.")
    assert "That 0 comes from blocking" in text


def test_no_blocking_credit_when_gated_was_broken_at_all(rich):  # noqa: F811
    raise_c002_effect(rich)
    body = rich["constraints"]["C-002"]
    body["blocked_before_call"] = True
    cells = body["boundaries"][FORMAT_V1_DELEGATION]["cells"]
    gated = next(c for c in cells if c["arm"] == "gated" and c["operator"] == "drop")
    gated.update({"violations": 3, "cvr": {"rate": 0.3, "lo": 0.1, "hi": 0.6}})

    assert "comes from blocking" not in verdict_text(site.render(rich))


# --- errored runs: per arm, and the per-run means cover graded runs only -----------------------


def test_errored_cost_is_listed_per_arm(rich):  # noqa: F811
    # Breaks if by_arm stays in the JSON only: a reader comparing per-run cost of two arms must
    # see which arm the money on errored runs belongs to.
    rich["runs"]["errored_cost"] = {
        "count": 3,
        "cost_usd": 0.35,
        "cost_unknown": 1,
        "by_arm": {
            "block_only": {"count": 1, "cost_usd": 0.1101, "cost_unknown": 0},
            "gated": {"count": 2, "cost_usd": 0.24, "cost_unknown": 1},
        },
    }

    text = text_of(site.render(rich))

    assert "spent on errored runs $0.350 (3 runs, 1 with no recorded cost)" in text
    assert "errored, Block-only $0.110 (1 run)" in text
    assert "errored, Gated (NemoGate) $0.240 (2 runs, 1 with no recorded cost)" in text


def test_the_per_run_cost_table_says_it_covers_graded_runs_only():
    # Breaks if the table lets a reader take a survivor-only mean for what the arm costs.
    text = text_of(site.render(metrics_page()))

    assert "Means cover graded runs only" in text
    assert "the money spent on errored runs, per arm," in text
    assert "Money in no rate" in text


# --- few runs: a range, not a 95% interval -------------------------------------------------


def test_the_mean_of_fewer_than_five_runs_is_labelled_a_range_not_an_interval():
    # Breaks if two runs' lowest and highest value is titled "95% bootstrap interval".
    page = site.render(metrics_page())
    table = metrics_table(page)

    assert 'title="95% bootstrap interval"' not in table
    assert 'title="Lowest to highest of the runs; too few runs for an interval"' in table


def test_the_mean_of_five_runs_keeps_its_bootstrap_interval():
    cell = with_metrics(
        make_cell("block_only", "drop", 5, 0, 5), cost=stat(0.14, 0.116, 0.164, 5, "bootstrap")
    )
    page = one_constraint({"delegation:a->b": {"cells": [cell], "comparisons": []}})

    table = metrics_table(site.render(page))

    assert "$0.140 ($0.116 to $0.164) mean of 5 runs" in text_of(table)
    assert 'title="95% bootstrap interval"' in table


def test_a_cell_from_before_basis_was_written_is_judged_by_its_run_count():
    old = {"n": 3, "mean": 0.2, "lo": 0.1, "hi": 0.3}  # no "basis" key
    cell = with_metrics(make_cell("block_only", "drop", 3, 0, 3), cost=old)
    page = one_constraint({"delegation:a->b": {"cells": [cell], "comparisons": []}})

    assert "(range $0.100 to $0.300)" in text_of(metrics_table(site.render(page)))


# --- runs whose carry-over could not be placed ---------------------------------------------


def test_runs_with_concurrent_task_calls_are_said_not_to_be_counted_in_carry_over_and_replans():
    blocks = {**BLOCKS, "n_ambiguous": 1}
    page = one_constraint(
        {
            "delegation:a->b": {
                "cells": [with_metrics(make_cell("block_only", "drop", 2, 0, 1), blocks=blocks)],
                "comparisons": [],
            }
        }
    )

    text = text_of(metrics_table(site.render(page)))

    assert "carry-over and re-plans leave out 1 run with several task calls at once" in text


def test_a_cell_where_every_run_is_ambiguous_shows_no_carry_over_number_and_does_not_crash():
    blocks = {
        **BLOCKS,
        "n_ambiguous": 2,
        "carry_over": stat(None, None, None, 0),
        "replan": None,
    }
    page = one_constraint(
        {
            "delegation:a->b": {
                "cells": [with_metrics(make_cell("block_only", "drop", 2, 0, 1), blocks=blocks)],
                "comparisons": [],
            }
        }
    )

    text = text_of(metrics_table(site.render(page)))

    assert "carried into a later delegation" not in text
    assert "carry-over and re-plans leave out 2 runs with several task calls at once" in text


def fork_page(n: int, n_forks: int | None) -> dict:
    cell = with_metrics(
        make_cell("gated", "drop", n, 0, n),
        cost=stat(0.05, 0.04, 0.06, n),
        wall_seconds={**stat(30.0, 20.0, 40.0, n), "unknown": 0},
        attempt_rate=None,
        blocks=BLOCKS,
    )
    if n_forks is not None:
        cell["n_forks"] = n_forks
    return one_constraint({"delegation:a->b": {"cells": [cell], "comparisons": []}})


def test_a_cell_of_forks_says_cost_and_time_are_after_the_fork_point():
    # Breaks if a paired sweep's fork cells print cost and time that read as whole-run numbers:
    # a fork's record holds only what came after the fork point (replay/README.md, "A fork
    # record is partial").
    text = text_of(metrics_table(site.render(fork_page(2, 2))))

    assert (
        "$0.050 (range $0.040 to $0.060) mean of 2 runs all forks: after the fork point only"
        in text
    )
    assert "30 s (range 20 to 40 s) mean of 2 runs all forks: after the fork point only" in text
    assert (
        "recovery: 4 steps after the first block (about $0.167 and 67 s, shared out by step)"
        in text
    )
    assert text.count("after the fork point") == 3


def test_a_cell_mixing_forks_and_whole_runs_says_how_many_are_partial():
    # Breaks if a cell that pools forks with independent runs reads as whole runs, or as all forks.
    text = text_of(metrics_table(site.render(fork_page(3, 1))))

    assert "mean of 3 runs 1 of 3 runs are forks: their cost is after the fork point only" in text
    assert "mean of 3 runs 1 of 3 runs are forks: their time is after the fork point only" in text


def test_a_cell_without_forks_or_from_an_older_file_has_no_fork_label():
    # Breaks if every cost cell is labelled, or a results file without n_forks is.
    for n_forks in (None, 0):
        text = text_of(metrics_table(site.render(fork_page(2, n_forks))))
        assert "fork" not in text


def runs_table(page: str) -> str:
    return re.search(r'<table class="cells">.*?</table>', page, flags=re.S).group(0)


def test_the_attempt_rate_is_a_column_of_the_runs_table_beside_violations_and_success():
    # Breaks if the attempt rate stays in the cost table: the reader would have to match rows
    # across two tables to see that a blocking arm's 0 violations hid 1 of 2 tries.
    text = text_of(runs_table(site.render(metrics_page())))

    assert "Violation rate Task passed (rule not considered) Attempted the rule's action" in text
    assert "50% (9–91%) 1 of 2 runs, blocked or not" in text
    assert "attempt" not in text_of(metrics_table(site.render(metrics_page()))).lower()


def test_a_cell_with_no_trace_or_no_metrics_says_so_in_the_attempt_column_not_zero():
    text = text_of(runs_table(site.render(metrics_page())))

    assert "Mutated drop" in text and "no trace to count from" in text  # metrics, no rate
    assert "Gated (NemoGate) drop" in text and "not in this results file" in text


def test_the_runs_table_has_no_attempt_column_for_a_file_without_arm_metrics():
    page = one_constraint(
        {"delegation:a->b": {"cells": [make_cell("natural", "none", 4, 0, 4)], "comparisons": []}}
    )

    assert "Attempted the rule" not in runs_table(site.render(page))


def gate_effect_page(attempts: dict | None) -> dict:
    cells = [
        with_metrics(
            make_cell("block_only", "none", 20, 0, 19),
            attempt_rate=rate(10, 20, 0.5, 0.2993, 0.7007),
        ),
        with_metrics(
            make_cell("gated", "none", 20, 0, 20), attempt_rate=rate(3, 20, 0.15, 0.0524, 0.3604)
        ),
    ]
    comparisons = [] if attempts is None else [attempts]
    return one_constraint({"delegation:a->b": {"cells": cells, "comparisons": comparisons}})


ATTEMPT_REDUCTION = {
    "name": "attempt_reduction",
    "operator": "none",
    "compaction": None,
    "a": {"arm": "gated", "operator": "none"},
    "b": {"arm": "block_only", "operator": "none"},
    "metric": "attempt_rate",
    "diff": -0.35,
    "lo": -0.5732,
    "hi": -0.0592,
    "n_a": 20,
    "n_b": 20,
}


def test_the_gate_effect_on_attempts_is_shown_with_its_interval_and_excludes_zero():
    # Breaks if the comparison stays in results.json only, or an interval below 0 is called
    # indistinguishable from it.
    text = text_of(site.render(gate_effect_page(ATTEMPT_REDUCTION)))

    assert "Gate effect on attempts (none)" in text
    assert "-35 pp (95% interval -57 pp to -6 pp) Lower than 0: the interval excludes 0." in text


def test_a_results_file_without_the_attempt_comparison_shows_no_empty_line_for_it():
    # Breaks if an older file (or a pair with nothing to count from) prints "not run" for a
    # comparison it never had.
    text = text_of(site.render(gate_effect_page(None)))

    assert "Gate effect on attempts" not in text
