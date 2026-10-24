"""report/cost.py: the price of one prevented rule break, from the cells of a results file.
Cells are built by hand and every expected figure is worked by hand in the comment above it."""

import pytest

from report import cost


def cell(
    arm, op, n, k, mean, lo=None, hi=None, *, cvr=(0.0, 1.0), forks=None, basis="bootstrap"
) -> dict:
    """A cell with `k` of `n` runs that broke the rule (95% interval `cvr`) and a cost stat."""
    out = {
        "arm": arm,
        "operator": op,
        "compaction": None,
        "n": n,
        "n_sources": n,
        "clustered": False,
        "violations": k,
        "cvr": {"rate": k / n, "lo": cvr[0], "hi": cvr[1]},
        "successes": n,
        "success": {"rate": 1.0, "lo": 0.0, "hi": 1.0},
        "cost_usd": 0.1,
        "arm_metrics": {
            "cost": {
                "n": n,
                "mean": mean,
                "lo": lo,
                "hi": hi,
                "basis": None if lo is None else basis,
            },
            "wall_seconds": {
                "n": 0,
                "mean": None,
                "lo": None,
                "hi": None,
                "basis": None,
                "unknown": n,
            },
            "attempt_rate": None,
            "blocks": None,
        },
    }
    if forks is not None:
        out["n_forks"] = forks
    return out


def pair(ungated=None, gated=None, block=None, op="drop") -> list[dict]:
    cells = [ungated or cell("mutated", op, 10, 8, 0.10, 0.08, 0.12, cvr=(0.55, 0.95))]
    cells.append(gated or cell("gated", op, 10, 2, 0.16, 0.13, 0.19, cvr=(0.05, 0.45)))
    if block is not None:
        cells.append(block)
    return cells


def only(rows):
    (row,) = rows
    return row


def test_the_price_is_extra_cost_per_run_over_violations_prevented_per_run():
    # Breaks if the price is cost over prevented PERCENT, over the gated rate alone, or a
    # difference of totals instead of means. Worked: prevented 8/10 - 2/10 = 0.6; extra
    # 0.16 - 0.10 = 0.06; price 0.06 / 0.6 = 0.10.
    rows, skipped = cost.price_condition(pair(), None, "drop")
    row = only(rows)

    assert skipped == 0
    assert row.arm == "gated"
    assert row.prevented == pytest.approx(0.6)
    assert row.extra == pytest.approx(0.06)
    assert row.price == pytest.approx(0.10)
    assert row.reason == ""
    assert (row.without.k, row.without.n, row.with_arm.k, row.with_arm.n) == (8, 10, 2, 10)


def test_the_widest_reading_combines_the_ends_of_the_two_intervals_the_worst_and_best_way():
    # Breaks if the bounds pair the wrong ends (the best price needs the least extra cost over
    # the most prevented). Worked: prevented lo 0.55 - 0.45 = 0.10, hi 0.95 - 0.05 = 0.90;
    # extra lo 0.13 - 0.12 = 0.01, hi 0.19 - 0.08 = 0.11; price lo 0.01 / 0.90, hi 0.11 / 0.10.
    row = only(cost.price_condition(pair(), None, "drop")[0])

    assert row.has_bounds
    assert row.prevented_lo == pytest.approx(0.10)
    assert row.prevented_hi == pytest.approx(0.90)
    assert row.extra_lo == pytest.approx(0.01)
    assert row.extra_hi == pytest.approx(0.11)
    assert row.price_lo == pytest.approx(0.01 / 0.90)
    assert row.price_hi == pytest.approx(1.10)


def test_a_bound_that_could_be_free_or_could_gain_nothing_is_open_not_a_number():
    # Breaks if a negative extra cost at the favourable end becomes a negative price, or if a
    # gain that might be 0 is divided by (an infinite upper price shown as a number). Worked:
    # extra lo 0.10 - 0.12 = -0.02 (the gate may cost less); prevented lo 0.50 - 0.50 = 0.
    ungated = cell("mutated", "drop", 10, 8, 0.10, 0.08, 0.12, cvr=(0.50, 0.95))
    gated = cell("gated", "drop", 10, 2, 0.16, 0.10, 0.19, cvr=(0.05, 0.50))
    row = only(cost.price_condition(pair(ungated, gated), None, "drop")[0])

    assert row.price == pytest.approx(0.10)
    assert row.price_lo is None
    assert row.price_hi is None
    assert row.has_bounds


def test_a_gate_that_cost_less_per_run_gets_words_and_no_price_never_a_negative_one():
    # Breaks if a negative extra cost is divided through into a negative "price". Worked:
    # extra 0.08 - 0.10 = -0.02, prevented 0.6.
    gated = cell("gated", "drop", 10, 2, 0.08, 0.06, 0.10, cvr=(0.05, 0.45))
    row = only(cost.price_condition(pair(gated=gated), None, "drop")[0])

    assert row.extra == pytest.approx(-0.02)
    assert row.price is None
    assert row.reason == "gated cost less per run"


def test_the_same_mean_cost_gets_no_price_and_says_so():
    # Breaks if a 0 extra cost reads as a free gate, "$0". Worked: 0.10 - 0.10 = 0.
    gated = cell("gated", "drop", 10, 2, 0.10, 0.08, 0.12, cvr=(0.05, 0.45))
    row = only(cost.price_condition(pair(gated=gated), None, "drop")[0])

    assert row.price is None
    assert row.reason == "no extra cost per run"


def test_no_violations_prevented_gets_a_dash_reason_and_never_divides_by_zero():
    # Breaks if prevented 0 divides (ZeroDivisionError or inf). Worked: 4/10 - 4/10 = 0.
    ungated = cell("mutated", "drop", 10, 4, 0.10, 0.08, 0.12)
    gated = cell("gated", "drop", 10, 4, 0.16, 0.13, 0.19)
    row = only(cost.price_condition(pair(ungated, gated), None, "drop")[0])

    assert row.prevented == 0
    assert row.price is None
    assert row.price_lo is None
    assert row.price_hi is None
    assert row.reason == "no violations prevented here"


def test_more_violations_with_the_gate_gets_no_price_even_when_the_extra_cost_is_positive():
    # Breaks if a negative rate difference with a positive extra cost yields a (negative or
    # flipped) price. Worked: 2/10 - 5/10 = -0.3; extra 0.06.
    ungated = cell("mutated", "drop", 10, 2, 0.10, 0.08, 0.12)
    gated = cell("gated", "drop", 10, 5, 0.16, 0.13, 0.19)
    row = only(cost.price_condition(pair(ungated, gated), None, "drop")[0])

    assert row.prevented == pytest.approx(-0.3)
    assert row.price is None
    assert row.reason == "more violations with the gate than without"


def test_block_only_is_priced_against_the_same_ungated_arm_after_gated():
    # Breaks if block-only is compared with the gated arm, or with another operator's cell.
    # Worked: prevented 8/10 - 1/10 = 0.7; extra 0.15 - 0.10 = 0.05; price 0.05 / 0.7.
    block = cell("block_only", "drop", 10, 1, 0.15, 0.12, 0.18, cvr=(0.02, 0.40))
    other = cell("mutated", "hedge", 10, 0, 9.0, 9.0, 9.0)
    rows, _ = cost.price_condition(pair(block=block) + [other], None, "drop")

    assert [r.arm for r in rows] == ["gated", "block_only"]
    assert rows[1].prevented == pytest.approx(0.7)
    assert rows[1].price == pytest.approx(0.05 / 0.7)


def test_with_no_sabotage_the_ungated_arm_is_the_natural_cell():
    # Breaks if the ungated arm at operator none is looked up as mutated (there is none).
    cells = [
        cell("natural", "none", 10, 6, 0.10, 0.08, 0.12, cvr=(0.3, 0.8)),
        cell("gated", "none", 10, 0, 0.12, 0.10, 0.14, cvr=(0.0, 0.3)),
    ]
    row = only(cost.price_condition(cells, None, "none")[0])

    assert row.prevented == pytest.approx(0.6)
    assert row.price == pytest.approx(0.02 / 0.6)


def test_whole_runs_against_forks_are_not_priced_and_are_counted():
    # Breaks if a natural source run (its whole cost) is compared with a fork (only what ran
    # after the handoff), which would make the gate look cheap.
    cells = [
        cell("natural", "none", 10, 6, 0.50, 0.40, 0.60, cvr=(0.3, 0.8)),
        cell("gated", "none", 10, 0, 0.12, 0.10, 0.14, cvr=(0.0, 0.3), forks=10),
        cell("block_only", "none", 10, 0, 0.13, 0.10, 0.15, cvr=(0.0, 0.3), forks=10),
    ]

    assert cost.price_condition(cells, None, "none") == ([], 2)


def test_a_cell_that_mixes_forks_and_whole_runs_is_not_priced():
    # Breaks if a cell with some forks (forks 4 of 10) is read as whole runs or as all forks.
    cells = [
        cell("mutated", "drop", 10, 8, 0.10, 0.08, 0.12, forks=4),
        cell("gated", "drop", 10, 2, 0.16, 0.13, 0.19, forks=4),
    ]

    assert cost.price_condition(cells, None, "drop") == ([], 1)


def test_forks_against_forks_are_priced_and_say_the_cost_is_after_the_fork_point():
    # Breaks if an all-fork pair is refused, or labelled as whole runs.
    cells = [
        cell("mutated", "drop", 10, 8, 0.10, 0.08, 0.12, cvr=(0.55, 0.95), forks=10),
        cell("gated", "drop", 10, 2, 0.16, 0.13, 0.19, cvr=(0.05, 0.45), forks=10),
    ]
    row = only(cost.price_condition(cells, None, "drop")[0])

    assert row.cover == "fork"
    assert only(cost.price_condition(pair(), None, "drop")[0]).cover == "full"


def test_fewer_than_five_runs_is_labelled_and_a_range_gives_no_price_bounds():
    # Breaks if a 4-run cell (cost on a lowest-to-highest range) is shown as a 95% interval, or
    # if bounds are built from a range.
    ungated = cell("mutated", "drop", 4, 3, 0.10, 0.08, 0.12, cvr=(0.3, 0.95), basis="range")
    gated = cell("gated", "drop", 4, 0, 0.16, 0.13, 0.19, cvr=(0.0, 0.5), basis="range")
    row = only(cost.price_condition(pair(ungated, gated), None, "drop")[0])

    assert row.small
    assert row.cost_is_range
    assert not row.has_bounds
    assert row.extra_lo is None
    assert row.price_lo is None
    assert row.price == pytest.approx(0.06 / 0.75)  # the point value is still shown


def test_ten_runs_on_a_bootstrap_basis_are_neither_small_nor_a_range():
    # Breaks if the labels are always on.
    row = only(cost.price_condition(pair(), None, "drop")[0])

    assert not row.small
    assert not row.cost_is_range


def test_a_cost_with_one_value_has_no_interval_so_no_bounds():
    # Breaks if a missing lo/hi is treated as 0 and gives a bound. The cell has 1 run.
    ungated = cell("mutated", "drop", 1, 1, 0.10, cvr=(0.2, 1.0))
    gated = cell("gated", "drop", 1, 0, 0.16, cvr=(0.0, 0.8))
    row = only(cost.price_condition(pair(ungated, gated), None, "drop")[0])

    assert row.small
    assert row.cost_is_range
    assert not row.has_bounds


def test_a_cell_without_arm_metrics_or_cost_gives_no_row_and_is_not_counted():
    # Breaks if an older results file gets a row, or a note about rows it never had.
    bare = pair()
    for c in bare:
        del c["arm_metrics"]
    assert cost.price_condition(bare, None, "drop") == ([], 0)

    nothing = pair()
    nothing[1]["arm_metrics"]["cost"] = {
        "n": 0,
        "mean": None,
        "lo": None,
        "hi": None,
        "basis": None,
    }
    assert cost.price_condition(nothing, None, "drop") == ([], 0)

    assert cost.price_condition([nothing[1]], None, "drop") == ([], 0)  # no ungated cell


def test_a_cell_without_its_rate_interval_gives_no_row():
    # Breaks if a missing cvr is replaced by a made-up interval.
    cells = pair()
    del cells[1]["cvr"]

    assert cost.price_condition(cells, None, "drop") == ([], 0)


def test_the_compaction_setting_picks_the_cells():
    # Breaks if a row mixes cells of two compaction settings.
    on = {"tokens": 4000, "keep": 3}
    cells = pair()
    other = pair(
        cell("mutated", "drop", 10, 5, 0.20, 0.18, 0.22, cvr=(0.2, 0.8)),
        cell("gated", "drop", 10, 0, 0.30, 0.28, 0.32, cvr=(0.0, 0.3)),
    )
    for c in other:
        c["compaction"] = on
    row = only(cost.price_condition(cells + other, on, "drop")[0])

    assert row.prevented == pytest.approx(0.5)
    assert row.extra == pytest.approx(0.10)
