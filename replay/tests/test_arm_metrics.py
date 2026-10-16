"""replay/arm_metrics.py and replay/intervals.py: per-cell cost, time, attempt and block numbers.

Expected values are hand-worked: the arithmetic is in the comments next to them.
"""

import pytest

from replay.arm_metrics import RunFacts, cell_metrics, errored_cost
from replay.blocks import RunBlocks
from replay.intervals import bootstrap_mean


def blocks(blocked, *, repeats=0, attempts=1, carry=0, replans=0, chances=0, after=0, total=40):
    return RunBlocks(
        blocked=blocked,
        repeats=repeats,
        attempts=attempts,
        carry_over=carry,
        replans=replans,
        replan_chances=chances,
        first_block_step=None if blocked == 0 else total - after,
        steps_after_first_block=after,
        total_steps=total,
    )


def ambiguous_blocks(blocked, attempts=1, after=10, total=40):
    """A run where two delegations were open at once: carry-over and re-plans are None."""
    return RunBlocks(
        blocked, 0, attempts, None, None, None, total - after, after, total, ambiguous=True
    )


# ---- intervals ------------------------------------------------------------------------------


def test_bootstrap_of_two_values_spans_both_of_them():
    # Resample means of [0, 10] are 0, 5 or 10; each end has probability 1/4, so the 2.5% and
    # 97.5% percentiles are 0 and 10. Breaks if the interval collapses to the mean or is
    # taken from the wrong end of the sorted resamples.
    assert bootstrap_mean([0.0, 10.0]) == (5.0, 0.0, 10.0)


def test_bootstrap_does_not_depend_on_input_order():
    # Breaks if the generator or the data order leaks into the file (results must be
    # byte-identical for the same runs in any order).
    assert bootstrap_mean([0.1, 0.4, 0.2, 0.9]) == bootstrap_mean([0.9, 0.2, 0.4, 0.1])


def test_bootstrap_of_one_value_has_no_interval():
    assert bootstrap_mean([0.25]) == (0.25, None, None)


def test_bootstrap_of_nothing_is_refused():
    with pytest.raises(ValueError, match="at least one"):
        bootstrap_mean([])


# ---- cost and time --------------------------------------------------------------------------


def test_cost_per_run_has_a_mean_and_an_interval():
    # Breaks if cost stays a per-cell sum (no per-run mean) or has no interval.
    facts = [RunFacts(True, 0.0, None, None), RunFacts(True, 0.5, None, None)]
    assert cell_metrics(facts)["cost"] == {
        "n": 2,
        "mean": 0.25,
        "lo": 0.0,
        "hi": 0.5,
        "basis": "range",
    }


def test_wall_time_counts_runs_without_a_timing_file_as_unknown_not_zero():
    # Breaks if an old run (no timing.json) drags the mean towards 0 or is dropped silently.
    facts = [
        RunFacts(True, 0.1, 10.0, None),
        RunFacts(True, 0.1, 30.0, None),
        RunFacts(True, 0.1, None, None),
    ]
    assert cell_metrics(facts)["wall_seconds"] == {
        "n": 2,
        "mean": 20.0,
        "lo": 10.0,
        "hi": 30.0,
        "basis": "range",
        "unknown": 1,
    }


def test_wall_time_with_no_timing_at_all_has_no_mean():
    wall = cell_metrics([RunFacts(True, 0.1, None, None)])["wall_seconds"]
    assert wall == {"n": 0, "mean": None, "lo": None, "hi": None, "basis": None, "unknown": 1}


def test_a_mean_of_fewer_than_five_values_shows_their_range_not_a_bootstrap_interval():
    # Breaks if 2 to 4 values get a "95%" bootstrap interval: with so few, the resample means pile
    # up on the lowest and highest value, so it is only the range and covers far less than 95%.
    # [1, 2, 3, 10] bootstraps to 1.5 to 8.0; the range, which is what is shown, is 1 to 10.
    cost = cell_metrics([RunFacts(True, c, None, None) for c in (1.0, 2.0, 3.0, 10.0)])["cost"]
    assert cost == {"n": 4, "mean": 4.0, "lo": 1.0, "hi": 10.0, "basis": "range"}


def test_a_mean_of_five_values_gets_the_bootstrap_interval():
    # bootstrap_mean([10, 12, 14, 16, 18]) is (14, 11.6, 16.4), inside the range 10 to 18
    cost = cell_metrics([RunFacts(True, c, None, None) for c in (10.0, 12.0, 14.0, 16.0, 18.0)])[
        "cost"
    ]
    assert cost["basis"] == "bootstrap"
    assert (cost["mean"], cost["lo"], cost["hi"]) == (14.0, 11.6, 16.4)


def test_a_single_value_has_no_interval_and_no_basis():
    cost = cell_metrics([RunFacts(True, 0.25, None, None)])["cost"]
    assert cost == {"n": 1, "mean": 0.25, "lo": None, "hi": None, "basis": None}


# ---- attempts and blocks --------------------------------------------------------------------


def mixed_cell():
    return [
        # A: blocked 3 times (1 repeat), 3 attempts, 1 carried over, re-planned 1 of 2 times,
        #    the last 10 of 40 events came after the first block; failed
        RunFacts(
            False,
            0.10,
            100.0,
            blocks(3, repeats=1, attempts=3, carry=1, replans=1, chances=2, after=10),
        ),
        # B: never attempted the forbidden action
        RunFacts(True, 0.20, 200.0, blocks(0, attempts=0)),
        # C: blocked once, no retry, 1 re-plan chance not taken; 20 of 40 events after; no time
        RunFacts(True, 0.30, None, blocks(1, attempts=1, chances=1, after=20)),
        # D: no readable trace: left out of every trace-based number
        RunFacts(True, 0.40, 50.0, None),
    ]


def test_attempt_rate_counts_runs_with_at_least_one_attempt_among_traced_runs():
    # traced A, B, C; attempts 3, 0, 1 -> 2 of 3 runs attempted. Wilson(2, 3) = 0.2077 to 0.9385.
    # Breaks if blocked-only runs count (B and the untraced D would then change n or k).
    assert cell_metrics(mixed_cell())["attempt_rate"] == {
        "k": 2,
        "n": 3,
        "rate": 0.6667,
        "lo": 0.2077,
        "hi": 0.9385,
    }


def test_block_counts_are_per_run_means_over_traced_runs():
    b = cell_metrics(mixed_cell())["blocks"]
    assert (b["n_traced"], b["runs_with_block"]) == (3, 2)
    assert b["blocked"]["n"] == 3
    assert b["blocked"]["mean"] == 1.3333  # (3 + 0 + 1) / 3
    assert b["repeats"]["mean"] == 0.3333  # (1 + 0 + 0) / 3
    assert b["carry_over"]["mean"] == 0.3333  # (1 + 0 + 0) / 3


def test_replan_rate_is_replans_over_chances_across_runs():
    # A: 1 of 2, C: 0 of 1 -> 1 of 3. Wilson(1, 3) = 0.0615 to 0.7923.
    assert cell_metrics(mixed_cell())["blocks"]["replan"] == {
        "k": 1,
        "n": 3,
        "rate": 0.3333,
        "lo": 0.0615,
        "hi": 0.7923,
    }


def test_stuck_rate_is_task_failure_among_runs_that_were_blocked():
    # blocked runs: A (failed), C (succeeded) -> 1 of 2. Wilson(1, 2) = 0.0945 to 0.9055.
    # Breaks if B (never blocked, succeeded) or D (untraced) are in the denominator.
    assert cell_metrics(mixed_cell())["blocks"]["stuck"] == {
        "k": 1,
        "n": 2,
        "rate": 0.5,
        "lo": 0.0945,
        "hi": 0.9055,
    }


def test_recovery_is_steps_and_a_pro_rata_share_of_cost_and_time():
    # A: 10 of 40 events after the first block -> 0.25 of $0.10 = $0.025, of 100 s = 25 s.
    # C: 20 of 40 -> 0.5 of $0.30 = $0.15; it has no timing, so no seconds.
    r = cell_metrics(mixed_cell())["blocks"]["recovery"]
    assert r["n"] == 2
    assert r["steps"]["mean"] == 15.0  # (10 + 20) / 2
    assert r["cost_usd_est"]["mean"] == 0.0875  # (0.025 + 0.15) / 2
    assert (r["seconds_est"]["n"], r["seconds_est"]["mean"]) == (1, 25.0)


def test_a_cell_with_no_block_has_no_stuck_rate_or_recovery():
    b = cell_metrics([RunFacts(True, 0.1, 5.0, blocks(0, attempts=0))])["blocks"]
    assert (b["runs_with_block"], b["stuck"], b["recovery"], b["replan"]) == (0, None, None, None)


def test_a_rule_without_a_tool_or_path_has_no_attempt_rate_but_still_has_blocks():
    no_notion = RunBlocks(2, 0, None, None, None, None, 5, 10, 30)
    m = cell_metrics([RunFacts(False, 0.1, None, no_notion)])
    assert m["attempt_rate"] is None
    assert m["blocks"]["blocked"]["mean"] == 2.0
    assert (m["blocks"]["carry_over"], m["blocks"]["replan"]) == (None, None)


def test_a_cell_with_no_traces_has_cost_and_time_but_no_block_numbers():
    m = cell_metrics([RunFacts(True, 0.1, None, None)])
    assert (m["attempt_rate"], m["blocks"]) == (None, None)
    assert m["cost"]["mean"] == 0.1


# ---- errored runs ---------------------------------------------------------------------------


def test_errored_cost_is_summed_per_arm_and_unknown_costs_are_counted_not_zeroed():
    # gated: 0.25 + 0.5 known, 1 unknown; block_only: 0.125 known.
    # Breaks if an unknown cost is added as 0 (the total then looks complete) or dropped.
    rows = [("gated", 0.25), ("gated", 0.5), ("gated", None), ("block_only", 0.125)]
    assert errored_cost(rows) == {
        "count": 4,
        "cost_usd": 0.875,
        "cost_unknown": 1,
        "by_arm": {
            "block_only": {"count": 1, "cost_usd": 0.125, "cost_unknown": 0},
            "gated": {"count": 3, "cost_usd": 0.75, "cost_unknown": 1},
        },
    }


def test_no_errored_runs_is_an_explicit_zero():
    assert errored_cost([]) == {"count": 0, "cost_usd": 0.0, "cost_unknown": 0, "by_arm": {}}


# ---- runs whose block numbers cannot be placed ------------------------------------------------


def test_ambiguous_runs_are_left_out_of_carry_over_and_replans_and_counted():
    # A: placed, carry 2, re-planned 1 of 2. B: ambiguous (two task calls in one turn), its
    # blocks still count towards blocked/stuck. Breaks if B's None is read as a 0 (carry-over mean
    # would be 1.0, re-plan 1 of 2 either way but n_ambiguous missing) or crashes the sum.
    cell = [
        RunFacts(False, 0.1, 10.0, blocks(2, attempts=2, carry=2, replans=1, chances=2, after=10)),
        RunFacts(True, 0.1, 10.0, ambiguous_blocks(1)),
    ]
    b = cell_metrics(cell)["blocks"]
    assert (b["n_traced"], b["n_ambiguous"], b["runs_with_block"]) == (2, 1, 2)
    assert b["carry_over"]["n"] == 1
    assert b["carry_over"]["mean"] == 2.0
    assert (b["replan"]["k"], b["replan"]["n"]) == (1, 2)
    assert b["blocked"]["mean"] == 1.5  # (2 + 1) / 2: blocked does not depend on placing
    assert b["stuck"]["n"] == 2


def test_a_cell_where_every_run_is_ambiguous_has_no_carry_over_or_replan_figure():
    b = cell_metrics([RunFacts(True, 0.1, None, ambiguous_blocks(1))])["blocks"]
    assert b["n_ambiguous"] == 1
    assert b["carry_over"]["n"] == 0
    assert b["carry_over"]["mean"] is None
    assert b["replan"] is None
