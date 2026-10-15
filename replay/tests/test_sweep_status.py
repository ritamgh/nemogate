"""replay/sweep_status.py: what a runs directory holds, and the top-up shortfall.

Run folders are written by hand, so every expected count is a literal.
"""

import json
from pathlib import Path

import pytest

from replay import sweep_status
from replay.sweep_status import main, scan, top_up_plan
from schemas.enums import Arm, Operator, Role
from schemas.run import RunRecord

BOUNDARY = "delegation:planner->coder"
HERE = {
    "model_ids": {Role.helper: "h1", Role.sut: "s1", Role.planner: "p1"},
    "safe_config_hash": "sc1",
    "ledger_hash": "lh1",
    "repo_sha": "a" * 40,
    "lockfile_hash": "lf1",
}


def put_record(runs, run_id, *, task="X1", arm="natural", operator="none", violated=False,
               success=True, **repro):  # fmt: skip
    fields = {**HERE, **repro}
    record = RunRecord(
        run_id=run_id,
        task_id=task,
        arm=arm,
        operator=operator,
        constraint_id="C-001",
        boundary=BOUNDARY,
        violations=("C-001",) if violated else (),
        task_success=success,
        tokens={role: 0 for role in Role},
        cost_usd=0.1,
        checkpoint_id=None,
        contree_image=None,
        **fields,
    )
    folder = runs / run_id
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(record.model_dump_json())
    return folder


def put_error(runs, run_id, *, task="X1", arm="natural", operator="none", **repro):
    fields = {**HERE, **repro}
    data = {
        "run_id": run_id,
        "task_id": task,
        "arm": arm,
        "operator": operator,
        **fields,
        "model_ids": {str(role): name for role, name in fields["model_ids"].items()},
        "error_type": "builtins.ValueError",
        "message": "boom",
        "traceback_tail": "",
    }
    folder = runs / run_id
    folder.mkdir(parents=True)
    (folder / "error.json").write_text(json.dumps(data))
    return folder


def plan(runs: Path, n: int, tasks=("X1",), **repro_overrides):
    found = scan(runs)
    cells = top_up_plan(
        found, {**HERE, **repro_overrides}, tasks, [Arm.natural], [Operator.none], n
    )
    return [(c.task_id, c.existing, c.planned) for c in cells]


def test_top_up_counts_records_and_errored_runs_as_attempts(tmp_path):
    # Breaks if an errored run is not an attempt: top-up would retry it, and a drop run that
    # had nothing to drop would be replaced by one where the rule was present (biased sample).
    put_record(tmp_path, "r-1")
    put_record(tmp_path, "r-2")
    put_error(tmp_path, "r-3")
    assert plan(tmp_path, 5) == [("X1", 3, 2)]


def test_top_up_plans_nothing_for_a_cell_that_is_full_or_over_full(tmp_path):
    for i in range(3):
        put_record(tmp_path, f"r-{i}")
    assert plan(tmp_path, 3) == [("X1", 3, 0)]
    assert plan(tmp_path, 2) == [("X1", 3, 0)]


def test_top_up_ignores_orphans_forks_and_other_reproducibility_fields(tmp_path):
    # Breaks if a half-written folder, a fork (not an independent sample), or a run from
    # another commit, ledger, lockfile, config or model counts toward this invocation's cell.
    put_record(tmp_path, "r-ok")
    (tmp_path / "r-orphan").mkdir()
    (tmp_path / "r-orphan" / "run_config.json").write_text("{}")
    fork = put_record(tmp_path, "r-fork")
    (fork / "fork.json").write_text(json.dumps({"source_run_id": "r-ok"}))
    put_record(tmp_path, "r-sha", repo_sha="b" * 40)
    put_record(tmp_path, "r-ledger", ledger_hash="other")
    put_record(tmp_path, "r-lock", lockfile_hash="other")
    put_record(tmp_path, "r-config", safe_config_hash="other")
    put_error(tmp_path, "r-err-sha", repo_sha="b" * 40)
    put_record(tmp_path, "r-model", model_ids={**HERE["model_ids"], Role.sut: "s2"})
    assert plan(tmp_path, 3) == [("X1", 1, 2)]


def test_top_up_counts_per_task_arm_and_operator(tmp_path):
    put_record(tmp_path, "r-1", task="X1", arm="mutated", operator="drop")
    put_record(tmp_path, "r-2", task="X2", arm="mutated", operator="drop")
    put_record(tmp_path, "r-3", task="X2", arm="mutated", operator="drop")
    cells = top_up_plan(
        scan(tmp_path),
        HERE,
        ["X1", "X2"],
        [Arm.natural, Arm.mutated],
        [Operator.none, Operator.drop],
        2,
    )
    assert [(c.task_id, c.arm, c.operator, c.existing, c.planned) for c in cells] == [
        ("X1", "natural", "none", 0, 2),
        ("X1", "mutated", "drop", 1, 1),
        ("X2", "natural", "none", 0, 2),
        ("X2", "mutated", "drop", 2, 0),
    ]


def test_scan_lists_orphans_and_leaves_out_forks_and_unrelated_folders(tmp_path):
    put_record(tmp_path, "r-ok")
    put_error(tmp_path, "r-err")
    (tmp_path / "r-empty").mkdir()
    damaged = put_record(tmp_path, "r-damaged")
    (damaged / "record.json").write_text('{"run_id": "r-damaged"}')
    bad_error = put_error(tmp_path, "r-bad-error")
    (bad_error / "error.json").write_text('{"task_id": "X1"}')
    fork = put_record(tmp_path, "r-fork")
    (fork / "fork.json").write_text("{}")
    (tmp_path / "notes").mkdir()
    (tmp_path / "spend.jsonl").write_text("x")
    found = scan(tmp_path)
    assert [a.run_id for a in found.attempts] == ["r-err", "r-ok"]
    assert found.orphans == ("r-bad-error", "r-damaged", "r-empty")
    assert found.forks == 1


def test_scan_of_a_missing_directory_is_empty(tmp_path):
    found = scan(tmp_path / "nope")
    assert (found.attempts, found.orphans, found.forks) == ((), (), 0)


def test_group_key_is_the_same_for_a_record_and_an_error_json_of_one_run(tmp_path):
    # Breaks if error.json's string role keys and the record's Role keys give different keys,
    # so errored runs never match their cell.
    put_record(tmp_path, "r-1")
    put_error(tmp_path, "r-2")
    one, two = scan(tmp_path).attempts
    assert one.group == two.group == sweep_status.group_key(HERE)


def test_status_prints_one_row_per_cell_and_group(tmp_path, capsys):
    put_record(tmp_path, "r-1", violated=True, success=False)
    put_record(tmp_path, "r-2", violated=False, success=True)
    put_error(tmp_path, "r-3")
    put_record(tmp_path, "r-4", arm="mutated", operator="drop", violated=True)
    put_record(tmp_path, "r-5", repo_sha="b" * 40)
    (tmp_path / "r-orphan").mkdir()
    assert main(["--runs-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out.splitlines()
    group_a = sweep_status.group_hash(sweep_status.group_key(HERE))
    group_b = sweep_status.group_hash(sweep_status.group_key({**HERE, "repo_sha": "b" * 40}))
    rows = [line.split() for line in out[1:4]]
    assert out[0].split() == [
        "task", "arm", "operator", "group", "records", "errored", "violations", "successes",
    ]  # fmt: skip
    assert sorted(rows) == sorted(
        [
            ["X1", "natural", "none", group_a, "2", "1", "1", "1"],
            ["X1", "natural", "none", group_b, "1", "0", "0", "1"],
            ["X1", "mutated", "drop", group_a, "1", "0", "1", "1"],
        ]
    )
    assert [r[1] for r in rows] == ["natural", "natural", "mutated"]  # arm order, not alphabetical
    assert "orphans (no record.json or error.json): 1" in out
    assert "  r-orphan" in out
    assert "forks (not counted): 0" in out
    assert any(line.strip().startswith(group_b) and "repo_sha=" + "b" * 12 in line for line in out)


def test_status_tasks_filter_and_missing_directory(tmp_path, capsys):
    put_record(tmp_path, "r-1", task="X1")
    put_record(tmp_path, "r-2", task="X2")
    assert main(["--runs-dir", str(tmp_path), "--tasks", "X2"]) == 0
    out = capsys.readouterr().out
    assert "X2" in out and "X1" not in out
    assert main(["--runs-dir", str(tmp_path / "nope")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_status_requires_a_runs_dir():
    with pytest.raises(SystemExit):
        main([])
