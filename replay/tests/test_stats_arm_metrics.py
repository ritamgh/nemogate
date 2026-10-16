"""replay/stats.py with per-arm metrics: loading timing.json, traces and errored-run cost, and the
`arm_metrics` block of every cell. Run folders are written by hand, so every number is a literal.
"""

import json
from pathlib import Path

from common.spend import append_spend, run_scope
from replay.stats import ErroredRun, aggregate, load_runs, main
from replay.tests.test_blocks import Trace
from replay.tests.test_stats import LEDGER, LEDGER_YAML, make_record
from schemas.enums import Role
from schemas.ledger import load_ledger_yaml
from schemas.trace import dump_event_jsonl


def write_run(root: Path, run_id: str, *, trace: Trace | None = None, timing=None, **kwargs):
    folder = root / run_id
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(make_record(run_id, **kwargs).model_dump_json())
    if trace is not None:
        (folder / "trace.jsonl").write_text(
            "".join(dump_event_jsonl(e) + "\n" for e in trace.events), encoding="utf-8"
        )
    if timing is not None:
        (folder / "timing.json").write_text(
            timing if isinstance(timing, str) else json.dumps(timing), encoding="utf-8"
        )
    return folder


def write_error(root: Path, run_id: str, *, arm="gated", **extra):
    folder = root / run_id
    folder.mkdir(parents=True)
    data = {"run_id": run_id, "arm": arm, "error_type": "builtins.ValueError", **extra}
    (folder / "error.json").write_text(json.dumps(data))
    return folder


def blocked_twice() -> Trace:
    return (
        Trace()
        .delegate("Edit frozen/a.py")
        .block("write_file", "frozen/a.py")
        .block("write_file", "frozen/a.py")
    )


def unblocked() -> Trace:
    return Trace().delegate("Edit app.py").call("write_file", "app.py")


# ---- loading --------------------------------------------------------------------------------


def test_load_runs_reads_wall_time_from_timing_json_and_the_trace(tmp_path):
    # Breaks if timing.json or trace.jsonl are not picked up (every wall time would be unknown).
    write_run(tmp_path, "r-a", trace=blocked_twice(), timing={"seconds": 137.5})
    (run,) = load_runs([tmp_path]).runs
    assert run.wall_seconds == 137.5
    assert [e.type for e in run.trace] == [
        "boundary_out",
        "boundary_in",
        "tool_call",
        "gate_block",
        "tool_call",
        "gate_block",
    ]


def test_a_run_without_timing_json_or_trace_is_still_a_readable_run_with_both_unknown(tmp_path):
    # Breaks if an old run folder (no sidecar) is called unreadable or given a made-up time.
    write_run(tmp_path, "r-old")
    loaded = load_runs([tmp_path])
    assert loaded.unreadable == 0
    assert (loaded.runs[0].wall_seconds, loaded.runs[0].trace) == (None, None)


def test_unusable_timing_json_means_unknown_time_not_an_unreadable_run(tmp_path):
    # a negative duration, text that is not JSON, and a missing key are all "unknown"
    write_run(tmp_path, "r-1", timing={"seconds": -3})
    write_run(tmp_path, "r-2", timing="not json")
    write_run(tmp_path, "r-3", timing={"start": "x"})
    loaded = load_runs([tmp_path])
    assert loaded.unreadable == 0
    assert [r.wall_seconds for r in loaded.runs] == [None, None, None]


def test_an_errored_run_brings_its_arm_and_the_cost_error_json_recorded(tmp_path):
    write_error(tmp_path, "r-e1", arm="gated", cost_usd=0.25)
    loaded = load_runs([tmp_path])
    assert (loaded.errored, loaded.errored_runs) == (1, (ErroredRun("gated", 0.25),))


def test_an_older_errored_run_takes_its_cost_from_the_spend_log_when_one_is_given(tmp_path):
    # error.json from before the runner recorded cost: the spend log is the only source.
    # Without --spend-log the cost is unknown (None), never 0.
    runs_dir = tmp_path / "runs"
    write_error(runs_dir, "r-e2", arm="block_only")
    log = tmp_path / "spend-copy.jsonl"
    with run_scope("r-e2"):
        append_spend(Role.sut, "m", 100, 50, 0.125, log_path=log)
        append_spend(Role.planner, "m", 10, 5, 0.25, log_path=log)
    assert load_runs([runs_dir]).errored_runs == (ErroredRun("block_only", None),)
    assert load_runs([runs_dir], spend_log=log).errored_runs == (ErroredRun("block_only", 0.375),)


def test_an_older_errored_run_with_no_line_in_the_spend_log_is_unknown_not_free(tmp_path):
    # Breaks if "the log has nothing for this run" is priced as $0: a mistyped --spend-log path,
    # a rotated log, or a log from the other machine would then report known, zero spend and
    # hide the "no recorded cost" caveat.
    runs_dir = tmp_path / "runs"
    write_error(runs_dir, "r-e3", arm="gated")
    other = tmp_path / "other-machine.jsonl"
    with run_scope("r-somebody-else"):
        append_spend(Role.sut, "m", 100, 50, 0.5, log_path=other)

    missing = load_runs([runs_dir], spend_log=tmp_path / "typo.jsonl")
    elsewhere = load_runs([runs_dir], spend_log=other)

    assert missing.errored_runs == (ErroredRun("gated", None),)
    assert elsewhere.errored_runs == (ErroredRun("gated", None),)


# ---- aggregation ----------------------------------------------------------------------------


def block_only_cell(tmp_path):
    write_run(
        tmp_path,
        "r-bo1",
        arm="block_only",
        operator="drop",
        trace=blocked_twice(),
        timing={"seconds": 100.0},
        success=False,
        cost=0.25,
    )
    write_run(
        tmp_path,
        "r-bo2",
        arm="block_only",
        operator="drop",
        trace=unblocked(),
        timing={"seconds": 200.0},
        cost=0.75,
    )
    loaded = load_runs([tmp_path])
    results = aggregate(
        loaded.runs, unreadable=0, ledger=LEDGER, extraction_recall=None, errored_runs=()
    )
    (cell,) = results["constraints"]["C-001"]["boundaries"]["delegation:planner->coder"]["cells"]
    return cell["arm_metrics"]


def test_a_cell_reports_per_run_cost_and_wall_time_with_intervals(tmp_path):
    # Cost 0.25 and 0.75 -> mean 0.5; a bootstrap of two values spans both (as in
    # test_arm_metrics). Breaks if the cell still only has a summed cost.
    m = block_only_cell(tmp_path)
    assert m["cost"] == {"n": 2, "mean": 0.5, "lo": 0.25, "hi": 0.75, "basis": "range"}
    assert m["wall_seconds"] == {
        "n": 2,
        "mean": 150.0,
        "lo": 100.0,
        "hi": 200.0,
        "basis": "range",
        "unknown": 0,
    }


def test_a_cell_reports_attempt_rate_blocks_and_stuck_from_the_traces(tmp_path):
    # bo1 blocked twice on frozen/a.py (1 repeat), failed; bo2 never touched frozen/.
    m = block_only_cell(tmp_path)
    assert m["attempt_rate"] == {"k": 1, "n": 2, "rate": 0.5, "lo": 0.0945, "hi": 0.9055}
    blocks = m["blocks"]
    assert (blocks["n_traced"], blocks["runs_with_block"]) == (2, 1)
    assert blocks["blocked"] == {"n": 2, "mean": 1.0, "lo": 0.0, "hi": 2.0, "basis": "range"}
    assert blocks["repeats"] == {"n": 2, "mean": 0.5, "lo": 0.0, "hi": 1.0, "basis": "range"}
    assert blocks["stuck"] == {"k": 1, "n": 1, "rate": 1.0, "lo": 0.2065, "hi": 1.0}
    # bo1: 6 events, the first block is event 2, so 4 of 6 follow it: $0.25 * 4/6, 100 s * 4/6
    assert blocks["recovery"]["steps"]["mean"] == 4.0
    assert blocks["recovery"]["cost_usd_est"]["mean"] == 0.1667
    assert blocks["recovery"]["seconds_est"]["mean"] == 66.6667


def test_runs_without_traces_leave_the_block_numbers_empty_but_keep_cost(tmp_path):
    write_run(tmp_path, "r-x", arm="gated", operator="drop", cost=0.5)
    loaded = load_runs([tmp_path])
    results = aggregate(loaded.runs, unreadable=0, ledger=LEDGER, extraction_recall=None)
    (cell,) = results["constraints"]["C-001"]["boundaries"]["delegation:planner->coder"]["cells"]
    m = cell["arm_metrics"]
    assert (m["attempt_rate"], m["blocks"]) == (None, None)
    assert m["cost"]["mean"] == 0.5
    assert m["wall_seconds"]["unknown"] == 1


def test_errored_run_cost_is_reported_next_to_the_run_counts_not_in_a_cell(tmp_path):
    # Breaks if the money spent on a run that raised is lost, or mixed into a graded cell.
    write_run(tmp_path, "r-ok", arm="gated", operator="drop", cost=0.5)
    write_error(tmp_path, "r-e1", arm="gated", cost_usd=0.25)
    write_error(tmp_path, "r-e2", arm="gated")
    loaded = load_runs([tmp_path])
    results = aggregate(
        loaded.runs,
        unreadable=0,
        errored=loaded.errored,
        ledger=LEDGER,
        extraction_recall=None,
        errored_runs=loaded.errored_runs,
    )
    assert results["runs"]["errored_cost"] == {
        "count": 2,
        "cost_usd": 0.25,
        "cost_unknown": 1,
        "by_arm": {"gated": {"count": 2, "cost_usd": 0.25, "cost_unknown": 1}},
    }
    (cell,) = results["constraints"]["C-001"]["boundaries"]["delegation:planner->coder"]["cells"]
    assert cell["cost_usd"] == 0.5


def test_results_are_format_version_2_and_the_cli_passes_the_spend_log(tmp_path, capsys):
    runs = tmp_path / "runs"
    write_error(runs, "r-e1", arm="gated")
    log = tmp_path / "log.jsonl"
    with run_scope("r-e1"):
        append_spend(Role.sut, "m", 1, 1, 0.5, log_path=log)
    ledger = tmp_path / "nemogate.yaml"
    ledger.write_text(LEDGER_YAML)
    out = tmp_path / "results.json"
    code = main([str(runs), "--ledger", str(ledger), "--spend-log", str(log), "--out", str(out)])
    assert code == 0, capsys.readouterr().err
    results = json.loads(out.read_text())
    assert results["format_version"] == 2
    assert results["runs"]["errored_cost"]["cost_usd"] == 0.5


RELAY_LEDGER = load_ledger_yaml(
    """
version: 1
constraints:
  - id: C-001
    text: Keep frozen/a.py as it is
    kind: constraint
    source: user
    authority: binding
    scope: [coder]
    origin: declared
    check: {type: path_forbidden, glob: "frozen/**", ops: [write, delete]}
  - id: C-002
    text: Do not change requirements.txt
    kind: constraint
    source: user
    authority: binding
    scope: [coder]
    origin: declared
    check: {type: files_frozen, globs: ["requirements.txt"]}
  - id: C-003
    text: Reviews end with a risk line
    kind: constraint
    source: user
    authority: binding
    scope: [reviewer]
    origin: declared
    check: {type: output_must_include, regex: "(?m)^RISK:", agent: reviewer}
  - id: C-004
    text: Be tidy
    kind: scope
    source: user
    authority: advisory
    scope: [coder]
    origin: declared
"""
)


def test_the_rule_wording_reaches_the_replan_count(tmp_path):
    # The relay has no cue word, so only the rule's own wording shows it is not an order.
    # Breaks if the ledger text is not passed to run_blocks.
    t = Trace().delegate("Edit frozen/a.py").block("write_file", "frozen/a.py")
    t.delegate("Keep frozen/a.py as it is.")
    write_run(tmp_path, "r-1", arm="block_only", operator="drop", trace=t)
    loaded = load_runs([tmp_path])
    results = aggregate(loaded.runs, unreadable=0, ledger=RELAY_LEDGER, extraction_recall=None)
    (cell,) = results["constraints"]["C-001"]["boundaries"]["delegation:planner->coder"]["cells"]
    assert (
        cell["arm_metrics"]["blocks"]["replan"]["k"],
        cell["arm_metrics"]["blocks"]["replan"]["n"],
    ) == (0, 1)


def test_each_constraint_says_whether_the_gate_blocks_it_before_the_call(tmp_path):
    # path rule: blocked; frozen files and output rules: graded after the run, never blocked;
    # no check: nothing to block. Breaks if the page is left to guess from the rule's kind.
    for n in range(1, 5):
        write_run(tmp_path, f"r-{n}", constraint=f"C-00{n}", arm="gated", operator="drop")
    runs = load_runs([tmp_path]).runs
    results = aggregate(runs, unreadable=0, ledger=RELAY_LEDGER, extraction_recall=None)
    got = {cid: c["blocked_before_call"] for cid, c in results["constraints"].items()}
    assert got == {"C-001": True, "C-002": False, "C-003": False, "C-004": False}
