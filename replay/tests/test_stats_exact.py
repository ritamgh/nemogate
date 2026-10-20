"""The exact tests in the stats card: which test a gated-vs-block_only comparison gets, from the
run folders, and what the results file says. The rule is `frozen/**` (write, delete).

Expected numbers are hand-worked literals (the arithmetic is next to each assertion).
"""

import copy
import json

import pytest

from replay.stats import LoadedRun, aggregate, main, render
from replay.tests.test_blocks import Trace
from replay.tests.test_stats import B1, LEDGER, dataset, make_record


def boundary_of(runs):
    got = aggregate(runs, unreadable=0, ledger=LEDGER, extraction_recall=None)
    return got["constraints"]["C-001"]["boundaries"][B1]


def exact_of(runs):
    return boundary_of(runs)["exact_tests"]


def pick(entries, name, operator):
    (entry,) = [e for e in entries if e["name"] == name and e["operator"] == operator]
    return entry


def gated(run_id, *, source=None, operator="none", violated=False, success=True, trace=None):
    record = make_record(run_id, arm="gated", operator=operator, violated=violated, success=success)
    return LoadedRun(
        record, fork_source=source, trace=None if trace is None else tuple(trace.events)
    )


def block_only(run_id, *, source=None, operator="none", violated=False, success=True, trace=None):
    record = make_record(
        run_id, arm="block_only", operator=operator, violated=violated, success=success
    )
    return LoadedRun(
        record, fork_source=source, trace=None if trace is None else tuple(trace.events)
    )


def independent(n, gated_broke, block_only_broke, operator="drop"):
    """Independent runs (no fork): the first `k` of n of each arm broke the rule."""
    return [
        gated(f"g-{operator}-{i}", operator=operator, violated=i < gated_broke) for i in range(n)
    ] + [
        block_only(f"b-{operator}-{i}", operator=operator, violated=i < block_only_broke)
        for i in range(n)
    ]


def forked(events_gated, events_block_only, operator="none"):
    """One fork per source in each arm; the lists say who broke the rule."""
    runs = []
    for i, (g, b) in enumerate(zip(events_gated, events_block_only, strict=True)):
        runs.append(gated(f"g-{operator}-{i}", source=f"src{i}", operator=operator, violated=g))
        runs.append(
            block_only(f"b-{operator}-{i}", source=f"src{i}", operator=operator, violated=b)
        )
    return runs


# ---------------------------------------------------------------- which test


def test_independent_cells_get_fisher_on_the_violation_counts():
    # gated 0 of 10 broke the rule, block_only 9 of 10: Fisher 2 * 10 / C(20,9) = 0.0001191.
    # Breaks if the comparison is missing or does not use the cells' own counts.
    entry = pick(exact_of(independent(10, 0, 9)), "violations", "drop")

    assert entry["test"] == "fisher"
    assert (entry["k_a"], entry["n_a"], entry["k_b"], entry["n_b"]) == (0, 10, 9, 10)
    assert entry["p_exact"] == pytest.approx(0.0001191, abs=1e-6)  # written to 6 decimals
    assert entry["a"] == {"arm": "gated", "operator": "drop"}
    assert entry["b"] == {"arm": "block_only", "operator": "drop"}
    assert entry["n_pairs"] is None and entry["p_exact_by_source"] is None


def test_independent_cells_never_get_mcnemar():
    # Breaks if any entry of a cell with no shared source is a paired test.
    entries = exact_of(independent(10, 3, 5))

    assert {e["test"] for e in entries} == {"fisher"}


def test_cells_of_forks_from_the_same_sources_get_mcnemar_and_never_fisher():
    # Ten sources, the gated fork broke the rule in none, the block_only fork in nine: nine
    # discordant pairs, all one way: 2 * 0.5^9 = 0.00390625 (Fisher would say 0.0001191).
    # Breaks if shared sources are treated as independent samples.
    entries = exact_of(forked([False] * 10, [True] * 9 + [False]))

    entry = pick(entries, "violations", "none")
    assert entry["test"] == "mcnemar"
    assert entry["n_pairs"] == 10 and entry["n_unmatched"] == 0
    assert entry["discordant"] == {"a_only": 0, "b_only": 9}
    assert entry["p_exact"] == pytest.approx(0.00390625, abs=1e-6)  # written to 6 decimals
    assert {e["test"] for e in entries} == {"mcnemar"}


def test_a_fork_whose_partner_arm_errored_is_left_out_and_counted():
    # block_only has no fork of the last source (that run errored, so it is not in the folders).
    # 4 pairs are matched, 1 gated fork is not. Breaks if the lone fork is counted as a pair, or
    # silently vanishes from the count.
    runs = forked([False] * 4 + [False], [True] * 4 + [False])
    runs = [r for r in runs if r.record.run_id != "b-none-4"]

    entry = pick(exact_of(runs), "violations", "none")

    assert entry["n_pairs"] == 4 and entry["n_unmatched"] == 1
    assert entry["discordant"] == {"a_only": 0, "b_only": 4}
    assert entry["p_exact"] == pytest.approx(0.125, abs=1e-12)  # 2 * 0.5^4


# ---------------------------------------------------------------- the three outcomes


def test_the_three_outcomes_are_tested_with_the_runs_own_flags():
    # Paired, 3 sources. Gated broke the rule in none and tried the action in s0 only.
    # block_only broke it in s0 and s1 and tried the action in s0 and s1. Every run finished the
    # task. So: violations b_only 2 (s0, s1); attempts b_only 1 (s1); done a_only 2 (gated is
    # done in all three, block_only only in s2).
    # Breaks if an outcome reads another outcome's flag (e.g. done = task_success alone, which
    # would count a run that finished and broke the rule).
    tried = Trace().delegate("Edit frozen/a.py").block("write_file", "frozen/a.py")
    clean = Trace().delegate("Edit app.py").call("write_file", "app.py")
    runs = []
    for i, (g_broke, b_broke, g_tried, b_tried) in enumerate(
        [(False, True, True, True), (False, True, False, True), (False, False, False, False)]
    ):
        runs.append(
            gated(f"g{i}", source=f"s{i}", violated=g_broke, trace=tried if g_tried else clean)
        )
        runs.append(
            block_only(f"b{i}", source=f"s{i}", violated=b_broke, trace=tried if b_tried else clean)
        )

    entries = exact_of(runs)

    assert [e["name"] for e in entries] == ["violations", "attempts", "done"]
    assert pick(entries, "violations", "none")["discordant"] == {"a_only": 0, "b_only": 2}
    assert pick(entries, "attempts", "none")["discordant"] == {"a_only": 0, "b_only": 1}
    # every run succeeded as a task, but the two violating block_only runs are not "done"
    assert pick(entries, "done", "none")["discordant"] == {"a_only": 2, "b_only": 0}


def test_attempts_are_left_out_when_a_side_has_no_trace():
    # Breaks if an unread trace is counted as "never tried" and a reduction nobody measured is
    # tested.
    runs = forked([False] * 3, [True] * 3)  # no traces at all

    assert [e["name"] for e in exact_of(runs)] == ["violations", "done"]


# ---------------------------------------------------------------- clustering and pooling


def test_pooled_entry_tests_all_operators_together_and_reports_the_source_level_test():
    # Operator "none" and operator "drop", the same four sources forked under each. Violations:
    #   src0: both operators, only block_only broke (b_only, b_only) -> net B
    #   src1: both operators, only block_only broke                  -> net B
    #   src2: "none" only block_only, "drop" only gated              -> tie
    #   src3: both operators, neither broke                          -> tie
    # discordant pairs: 5 b_only, 1 a_only -> n = 6, k = 1: 2 * (1 + 6) / 64 = 0.21875
    # sources: 2 B, 0 A, 2 ties -> n = 2, k = 0: 2 * 1 / 4 = 0.5
    # Breaks if there is no pooled entry, if it counts the 6 pairs as 6 independent sources (the
    # clustering the source-level test exists to show), or if a tie is counted for a side.
    rows = {
        "none": ([False, False, False, False], [True, True, True, False]),
        "drop": ([False, False, True, False], [True, True, False, False]),
    }
    runs = []
    for operator, (g, b) in rows.items():
        runs += forked(g, b, operator=operator)

    entries = exact_of(runs)
    pooled = pick(entries, "violations", "pooled")

    assert pooled["test"] == "mcnemar"
    assert pooled["n_pairs"] == 8 and pooled["n_sources"] == 4
    assert pooled["discordant"] == {"a_only": 1, "b_only": 5}
    assert pooled["p_exact"] == pytest.approx(0.21875, abs=1e-12)
    assert pooled["by_source"] == {"a_only": 0, "b_only": 2, "ties": 2}
    assert pooled["p_exact_by_source"] == pytest.approx(0.5, abs=1e-12)
    assert pooled["a"] == {"arm": "gated", "operator": "pooled"}


def test_per_operator_entries_stay_when_pooled_is_added():
    # Breaks if pooling replaces the per-operator rows instead of adding to them.
    entries = exact_of(
        forked([False] * 4, [True] * 4, "none") + forked([False] * 5, [True] * 5, "drop")
    )

    assert pick(entries, "violations", "none")["discordant"] == {"a_only": 0, "b_only": 4}
    assert pick(entries, "violations", "drop")["discordant"] == {"a_only": 0, "b_only": 5}
    # src0..src3 repeat under both operators: 4 sources of 9 pairs (src4 only under "drop")
    assert pick(entries, "violations", "pooled")["n_pairs"] == 9
    assert pick(entries, "violations", "pooled")["n_sources"] == 5


def test_there_is_no_pooled_entry_for_a_single_operator_or_for_independent_cells():
    # Breaks if a pooled row repeats the only operator's row, or pools independent operator cells.
    assert [e["operator"] for e in exact_of(forked([False] * 3, [True] * 3))] == ["none", "none"]
    both = independent(10, 0, 9, "drop") + independent(10, 1, 8, "hedge")
    assert "pooled" not in {e["operator"] for e in exact_of(both)}


def test_cells_with_other_compaction_settings_are_tested_separately():
    # Runs under different settings are not one condition; a gated cell is tested against the
    # block_only cell of its own setting. The gated cell with the summariser on has no partner, so
    # it has no entry. Breaks if the partner is found by arm and operator alone (the unpartnered
    # cell would be tested against the wrong runs, giving two violation entries).
    def run(make, name, i, knobs, **kwargs):
        record = make_record(f"{name}{i}", arm=make, operator="drop", **kwargs)
        return LoadedRun(record, knobs=knobs)

    off, on = (None, None), (100, 6)
    runs = (
        [run("gated", "g", i, off) for i in range(4)]
        + [run("block_only", "b", i, off, violated=True) for i in range(4)]
        + [run("gated", "gc", i, on) for i in range(4)]
    )

    (entry,) = [e for e in exact_of(runs) if e["name"] == "violations"]

    assert entry["compaction"] == {"tokens": None, "keep": None}
    assert (entry["k_a"], entry["n_a"], entry["k_b"], entry["n_b"]) == (0, 4, 4, 4)


# ---------------------------------------------------------------- the results file


def test_without_a_gated_and_block_only_pair_the_file_has_no_exact_tests_key():
    # The file a sweep with no blocking arm gave before this change is the file it gives now.
    # Breaks if an empty key is written, which would change every older boundary.
    runs = [run for run, _ in dataset() if run.record.arm.value != "gated"]

    assert "exact_tests" not in boundary_of(runs)


def test_the_file_is_the_same_in_any_input_order():
    # Breaks if pairing or pooling depends on the order the folders were read in.
    runs = forked([False, True, False, False], [True, True, False, True]) + forked(
        [False, False], [True, False], "drop"
    )

    a = render(aggregate(runs, unreadable=0, ledger=LEDGER, extraction_recall=None))
    b = render(aggregate(list(reversed(runs)), unreadable=0, ledger=LEDGER, extraction_recall=None))

    assert a == b


def test_old_results_without_exact_tests_still_render_unchanged():
    # A results.json written before this change has no `exact_tests`. The page reads it as it did,
    # and a file that has the key renders the same page (the key is optional and ignored).
    # Breaks if the page needs the key, or a new key changes what an old field shows.
    from pathlib import Path

    from report import site

    fixture = Path(__file__).parents[2] / "report" / "tests" / "fixtures" / "results_rich.json"
    old = json.loads(fixture.read_text(encoding="utf-8"))
    new = copy.deepcopy(old)
    for constraint in new["constraints"].values():
        for boundary in constraint["boundaries"].values():
            boundary["exact_tests"] = []

    assert "exact_tests" not in json.dumps(old)
    assert site.render(old) == site.render(new)


def test_cli_writes_the_exact_tests_into_results_json(tmp_path):
    # End to end through the run folders: fork.json marks the forks, so the file carries a paired
    # test. Breaks if the loader's fork source does not reach the pairing.
    from replay.tests.test_stats import write_run

    ledger = tmp_path / "ledger.yaml"
    from replay.tests import test_stats

    ledger.write_text(test_stats.LEDGER_YAML, encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    for run in forked([False] * 3, [True] * 3):
        write_run(runs, run, is_fork=True)
    out = tmp_path / "results.json"

    assert main([str(runs), "--ledger", str(ledger), "--out", str(out)]) == 0

    entries = json.loads(out.read_text())["constraints"]["C-001"]["boundaries"][B1]["exact_tests"]
    entry = pick(entries, "violations", "none")
    assert entry["test"] == "mcnemar"
    assert entry["discordant"] == {"a_only": 0, "b_only": 3}
    assert entry["p_exact"] == pytest.approx(0.25, abs=1e-12)  # 2 * 0.5^3
