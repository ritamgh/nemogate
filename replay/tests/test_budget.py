import json

import pytest

from replay.app import pairs
from replay.budget import SHARES, Plan, main, mean_cost, plan, runs_per_cell
from schemas.config import BudgetConfig, load_config
from schemas.enums import Arm, Operator, Role
from schemas.run import RunRecord

CONFIG = load_config("config.yaml").model_copy(
    update={"budget": BudgetConfig(total_usd=60, stop_at_fraction=0.8)}
)


def _record(run_id: str, cost: float) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        task_id="x",
        arm=Arm.natural,
        operator=Operator.none,
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


def _plan(spent_usd: float) -> Plan:
    return plan(
        bucket="kill_gate",
        cost_per_run=0.05,
        arms=[Arm.natural, Arm.mutated],
        operators=[Operator.none, Operator.drop],
        n_tasks=2,
        config=CONFIG,
        spent_usd=spent_usd,
    )


def test_shares_are_the_spec_budget_split():
    # Breaks if a bucket share drifts from the SPEC "Budget" table.
    assert SHARES == {
        "dev": 0.20,
        "kill_gate": 0.10,
        "main_sweep": 0.40,
        "repair": 0.15,
        "final": 0.15,
    }


def test_runs_per_cell_floors_bucket_over_cell_cost():
    # 6.0 / (0.05 * 2 pairs * 2 tasks) = 30. Breaks if pairs or tasks are left out of the divisor.
    assert runs_per_cell(6.0, 0.05, 2, 2) == 30
    # 6.0 / (1.0 * 1 * 4) = 1.5 floors to 1, never rounds up past the bucket.
    assert runs_per_cell(6.0, 1.0, 1, 4) == 1


@pytest.mark.parametrize(
    "args",
    [(0, 0.05, 2, 2), (6.0, 0, 2, 2), (6.0, 0.05, 0, 2), (6.0, 0.05, 2, 0), (6.0, -1, 2, 2)],
)
def test_runs_per_cell_rejects_non_positive_inputs(args):
    # Breaks if a zero cost divides by zero or a negative input yields a negative count.
    with pytest.raises(ValueError):
        runs_per_cell(*args)


def test_plan_takes_bucket_share_of_total_and_excludes_impossible_pairs():
    p = _plan(spent_usd=0.0)
    # natural x drop and mutated x none are not run: 2 pairs, not 4.
    assert p.bucket == "kill_gate"
    assert p.bucket_usd == pytest.approx(6.0)
    assert p.pairs == 2
    assert p.n_tasks == 2
    assert p.n == 30
    assert p.planned_usd == pytest.approx(6.0)
    assert p.stop_usd == pytest.approx(48.0)
    assert p.capped is False


def test_plan_lowers_n_when_spent_is_near_the_stop_line():
    # 47.0 spent leaves 1.0 below the 48.0 line; one repetition costs 0.05*2*2 = 0.20 -> 5.
    p = _plan(spent_usd=47.0)
    assert p.n == 5
    assert p.planned_usd == pytest.approx(1.0)
    assert p.capped is True


def test_plan_is_not_capped_when_the_bucket_just_fits_below_the_line():
    # 42.0 spent + 6.0 planned lands exactly on 48.0: allowed, not capped.
    p = _plan(spent_usd=42.0)
    assert p.n == 30
    assert p.capped is False


@pytest.mark.parametrize("spent", [48.0, 55.0])
def test_plan_is_zero_at_or_past_the_stop_line(spent):
    p = _plan(spent_usd=spent)
    assert p.n == 0
    assert p.planned_usd == 0.0
    assert p.capped is True


def test_plan_with_no_pairs_is_an_error():
    # Natural with drop leaves nothing to run.
    with pytest.raises(ValueError):
        plan(
            bucket="dev",
            cost_per_run=0.05,
            arms=[Arm.natural],
            operators=[Operator.drop],
            n_tasks=1,
            config=CONFIG,
            spent_usd=0.0,
        )


def _blocking_plan(**kw) -> Plan:
    return plan(
        bucket="kill_gate",
        cost_per_run=0.05,
        arms=[Arm.block_only, Arm.gated],
        operators=[Operator.drop],
        n_tasks=1,
        config=CONFIG,
        spent_usd=0.0,
        **kw,
    )


def test_plan_of_a_paired_sweep_pays_for_the_natural_source_of_every_fork():
    # 6.0 / (0.05 * (2 forks + 1 natural source) * 1 task) = 40. Breaks if the sources are left
    # out of the divisor (that gives 60 and spends a third more than the check printed).
    p = _blocking_plan(paired=True)
    assert (p.pairs, p.source_runs, p.n) == (2, 1, 40)
    assert p.planned_usd == pytest.approx(6.0)


def test_plan_of_independent_runs_has_no_source_runs():
    # Breaks if the plan assumes paired forks unless told otherwise: the runner and the repair
    # loop make independent runs by default, so that would price them 50% too high.
    p = _blocking_plan()
    assert (p.pairs, p.source_runs, p.n) == (2, 0, 60)


def test_plan_does_not_pay_twice_for_a_natural_arm_that_is_the_source():
    # natural x none is a pair of its own, and it is the source: 2 pairs, no extra run.
    p = plan(
        bucket="kill_gate",
        cost_per_run=0.05,
        arms=[Arm.natural, Arm.mutated],
        operators=[Operator.none, Operator.drop],
        n_tasks=1,
        config=CONFIG,
        spent_usd=0.0,
    )
    assert (p.pairs, p.source_runs, p.n) == (2, 0, 60)


def test_plan_rejects_an_unknown_bucket():
    with pytest.raises(ValueError):
        plan(
            bucket="nope",
            cost_per_run=0.05,
            arms=[Arm.natural],
            operators=[Operator.none],
            n_tasks=1,
            config=CONFIG,
            spent_usd=0.0,
        )


def test_mean_cost_is_the_arithmetic_mean():
    records = [_record("r-1", 0.10), _record("r-2", 0.20), _record("r-3", 0.45)]
    assert mean_cost(records) == pytest.approx(0.25)
    assert mean_cost(iter(records[:1])) == pytest.approx(0.10)


def test_mean_cost_of_nothing_is_an_error():
    with pytest.raises(ValueError):
        mean_cost([])


@pytest.fixture
def spend_log(tmp_path, monkeypatch):
    log = tmp_path / "spend.jsonl"
    log.write_text("")
    monkeypatch.setenv("NEMOGATE_SPEND_LOG", str(log))
    return log


KILL_GATE = ["--bucket", "kill_gate", "--arms", "natural", "mutated", "--operators", "none", "drop"]


def test_cli_prints_one_fact_per_line_ending_with_n(spend_log, capsys):
    code = main([*KILL_GATE, "--tasks", "A", "B", "--cost-per-run", "0.05"])
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert lines[-1] == "N per arm per task: 30"
    assert "bucket: kill_gate" in lines
    assert "pairs: 2" in lines
    assert "tasks: 2" in lines
    assert "capped: no" in lines


def test_cli_exits_1_when_not_even_one_run_fits(spend_log, capsys):
    # 6.0 / (5.0 * 2 * 2) = 0.3 -> 0 runs.
    code = main([*KILL_GATE, "--tasks", "A", "B", "--cost-per-run", "5.0"])
    assert code == 1
    assert capsys.readouterr().out.splitlines()[-1] == "N per arm per task: 0"


def test_cli_reads_spent_from_the_spend_log(spend_log, capsys):
    spend_log.write_text('{"kind": "settle", "call_id": "a", "role": "sut", "cost_usd": 47.0}\n')
    code = main([*KILL_GATE, "--tasks", "A", "B", "--cost-per-run", "0.05"])
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert "capped: yes" in lines
    assert lines[-1] == "N per arm per task: 5"


def test_cli_derives_cost_per_run_from_run_records(spend_log, tmp_path, capsys):
    runs = tmp_path / "runs"
    for name, cost in (("r-1", 0.04), ("r-2", 0.06)):
        (runs / name).mkdir(parents=True)
        (runs / name / "record.json").write_text(
            json.dumps(_record(name, cost).model_dump(mode="json"))
        )
    code = main([*KILL_GATE, "--tasks", "A", "B", "--from-runs", str(runs)])
    assert code == 0
    assert capsys.readouterr().out.splitlines()[-1] == "N per arm per task: 30"


def test_cli_requires_exactly_one_cost_source(spend_log, tmp_path):
    with pytest.raises(SystemExit):
        main([*KILL_GATE, "--tasks", "A"])
    with pytest.raises(SystemExit):
        main([*KILL_GATE, "--tasks", "A", "--cost-per-run", "0.05", "--from-runs", str(tmp_path)])


def test_cli_counts_the_natural_sources_only_of_a_paired_sweep(spend_log, capsys):
    blocking = ["--bucket", "kill_gate", "--arms", "block_only", "gated", "--operators", "drop"]
    assert main([*blocking, "--tasks", "T", "--cost-per-run", "0.05", "--paired"]) == 0
    paired = capsys.readouterr().out.splitlines()
    assert "extra natural source runs per repetition: 1" in paired
    assert paired[-1] == "N per arm per task: 40"
    assert main([*blocking, "--tasks", "T", "--cost-per-run", "0.05"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "N per arm per task: 60"


def test_cli_from_runs_averages_full_runs_and_leaves_fork_records_out(spend_log, tmp_path, capsys):
    # A fork's cost_usd covers only the part after the boundary. Breaks if the mean includes
    # it: (0.20 + 0.05) / 2 = 0.125 gives N 24 where the full runs' 0.20 gives N 15.
    runs = tmp_path / "runs"
    for name, cost, is_fork in (("r-full", 0.20, False), ("r-fork", 0.05, True)):
        (runs / name).mkdir(parents=True)
        (runs / name / "record.json").write_text(
            json.dumps(_record(name, cost).model_dump(mode="json"))
        )
        if is_fork:
            (runs / name / "fork.json").write_text("{}")
    assert main([*KILL_GATE, "--tasks", "A", "--from-runs", str(runs)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "cost per run usd: 0.2000" in lines
    assert lines[-1] == "N per arm per task: 15"


def test_cli_from_runs_with_only_fork_records_has_nothing_to_average(spend_log, tmp_path):
    runs = tmp_path / "runs"
    (runs / "r-fork").mkdir(parents=True)
    (runs / "r-fork" / "record.json").write_text(
        json.dumps(_record("r-fork", 0.05).model_dump(mode="json"))
    )
    (runs / "r-fork" / "fork.json").write_text("{}")
    with pytest.raises(SystemExit):
        main([*KILL_GATE, "--tasks", "A", "--from-runs", str(runs)])


def test_plan_accepts_the_blocking_arms_with_operator_none():
    # Breaks if gated or block_only x none is refused: the budget check would print "no pair
    # to run" for the sweep that measures the gate on natural loss. 2 pairs, 6.0 / (0.05 * 2) = 60
    # (independent runs, no source run).
    p = plan(
        bucket="kill_gate",
        cost_per_run=0.05,
        arms=[Arm.block_only, Arm.gated],
        operators=[Operator.none],
        n_tasks=1,
        config=CONFIG,
        spent_usd=0.0,
    )
    assert (p.pairs, p.n) == (2, 60)


def test_pairs_keeps_natural_unmutated_and_mutated_mutated():
    # Breaks if opening gated x none also lets natural run an operator or mutated run without
    # one (mutated x none is just the natural arm under another name).
    arms = [Arm.natural, Arm.mutated, Arm.block_only, Arm.gated]
    assert pairs(arms, [Operator.none, Operator.drop]) == [
        (Arm.natural, Operator.none),
        (Arm.mutated, Operator.drop),
        (Arm.block_only, Operator.none),
        (Arm.block_only, Operator.drop),
        (Arm.gated, Operator.none),
        (Arm.gated, Operator.drop),
    ]
