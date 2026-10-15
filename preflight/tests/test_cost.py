import pytest

from preflight.cost import main, summarize
from schemas.enums import Arm, Operator, Role
from schemas.run import RunRecord


def _record(run_id: str, arm: Arm, cost: float) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        task_id="x",
        arm=arm,
        operator=Operator.none if arm == Arm.natural else Operator.drop,
        constraint_id="C-001",
        boundary="delegation:a->b",
        violations=(),
        task_success=True,
        tokens={role: 0 for role in Role},
        cost_usd=cost,
        checkpoint_id=None,
        contree_image=None,
        model_ids={role: "m" for role in Role},
        safe_config_hash="h",
        ledger_hash="h",
        repo_sha="h",
        lockfile_hash="h",
    )


# natural 0.10, 0.20, 0.60: n 3, mean 0.30, median 0.20, max 0.60, total 0.90.
# mutated 0.05, 0.15:       n 2, mean 0.10, median 0.10, max 0.15, total 0.20.
# all five:                 n 5, mean 0.22, median 0.15, max 0.60, total 1.10.
RECORDS = [
    _record("r-1", Arm.natural, 0.10),
    _record("r-2", Arm.mutated, 0.05),
    _record("r-3", Arm.natural, 0.60),
    _record("r-4", Arm.mutated, 0.15),
    _record("r-5", Arm.natural, 0.20),
]


def test_summarize_groups_by_arm_with_hand_checked_numbers():
    # Breaks if the mean/median/max are computed over the wrong group, or an arm is mixed in
    # with another (the mutated arm's partial-run costs would drag the natural mean down).
    stats = summarize(RECORDS)
    assert list(stats) == ["natural", "mutated", "all"]
    assert (stats["natural"].n, stats["natural"].max) == (3, 0.60)
    assert stats["natural"].mean == pytest.approx(0.30)
    assert stats["natural"].median == pytest.approx(0.20)
    assert stats["natural"].total == pytest.approx(0.90)
    assert (stats["mutated"].n, stats["mutated"].max) == (2, 0.15)
    assert stats["mutated"].mean == pytest.approx(0.10)
    assert stats["mutated"].median == pytest.approx(0.10)
    assert (stats["all"].n, stats["all"].max) == (5, 0.60)
    assert stats["all"].mean == pytest.approx(0.22)
    assert stats["all"].median == pytest.approx(0.15)


def test_summarize_of_nothing_is_an_error():
    # Breaks if an empty folder prints a table of zeros that budget planning would trust.
    with pytest.raises(ValueError, match="no run records"):
        summarize([])


def _write_runs(parent, records):
    for record in records:
        folder = parent / record.run_id
        folder.mkdir(parents=True)
        (folder / "record.json").write_text(record.model_dump_json(), encoding="utf-8")


def test_cli_reads_a_folder_of_runs_and_single_run_folders_together(tmp_path, capsys):
    # Breaks if either layout is not found: a parent of run folders (replay.budget's layout) or
    # a run folder itself.
    _write_runs(tmp_path / "runs", RECORDS[:4])
    _write_runs(tmp_path / "other", RECORDS[4:])
    rc = main([str(tmp_path / "runs"), str(tmp_path / "other" / "r-5")])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].split() == ["natural", "3", "0.3000", "0.2000", "0.6000", "0.9000"]
    assert lines[2].split() == ["mutated", "2", "0.1000", "0.1000", "0.1500", "0.2000"]
    assert lines[3].split() == ["all", "5", "0.2200", "0.1500", "0.6000", "1.1000"]


def test_cli_names_a_folder_without_records(tmp_path):
    # Breaks if a typo in the path prints an empty table instead of an error.
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit) as exit_info:
        main([str(tmp_path / "empty")])
    assert exit_info.value.code == 2


def test_cli_names_an_invalid_record(tmp_path, capsys):
    # Breaks if a half-written record.json is skipped, which would understate the cost.
    folder = tmp_path / "r-bad"
    folder.mkdir()
    (folder / "record.json").write_text('{"run_id": "r-bad"}', encoding="utf-8")
    with pytest.raises(SystemExit):
        main([str(tmp_path)])
    assert "r-bad/record.json is not a valid run record" in capsys.readouterr().err
