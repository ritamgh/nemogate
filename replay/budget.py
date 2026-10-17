"""How many runs per arm per task a budget bucket pays for, capped by the spend stop line.

`python -m replay.budget` prints the plan and makes no model call and no sandbox call.
"""

import argparse
import math
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from common.spend import total_spent
from replay.app import pairs
from replay.runner import default_refusals
from schemas.config import AppConfig, load_config
from schemas.enums import Arm, Operator
from schemas.run import RunRecord

# docs/SPEC.md "Budget": a bucket's amount is its share of config.budget.total_usd.
SHARES = {"dev": 0.20, "kill_gate": 0.10, "main_sweep": 0.40, "repair": 0.15, "final": 0.15}

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
_EPS = 1e-9  # absorbs float error so an exact fit (6.0 / 0.2) does not floor one run short


@dataclass(frozen=True)
class Plan:
    bucket: str
    bucket_usd: float
    cost_per_run: float
    pairs: int  # number of (arm, operator) pairs per task that run for at least one task
    source_runs: int  # natural runs per task and repetition that only exist to be forked: 0 or 1
    n_tasks: int
    runs: int  # runs per repetition, every task, refused cells left out and sources included
    n: int  # runs per arm per task
    planned_usd: float
    spent_usd: float
    stop_usd: float
    capped: bool  # n was lowered to stay under stop_usd


def _floor(x: float) -> int:
    return max(0, math.floor(x + _EPS))


def runs_per_cell(bucket_usd: float, cost_per_run: float, n_pairs: int, n_tasks: int) -> int:
    if not (bucket_usd > 0 and cost_per_run > 0 and n_pairs > 0 and n_tasks > 0):
        raise ValueError(
            "bucket_usd, cost_per_run, n_pairs and n_tasks must all be > 0, got "
            f"{bucket_usd}, {cost_per_run}, {n_pairs}, {n_tasks}"
        )
    return _floor(bucket_usd / (cost_per_run * n_pairs * n_tasks))


def plan(
    *,
    bucket: str,
    cost_per_run: float,
    arms: Sequence[Arm],
    operators: Sequence[Operator],
    n_tasks: int,
    config: AppConfig,
    spent_usd: float,
    paired: bool = False,
    refused: Collection[tuple[int, Arm, Operator]] = (),
) -> Plan:
    """`refused` holds (task index, arm, operator) cells the runner refuses (`replay.runner.
    refusals`); they are left out of the count. A paired sweep's refused source comes with its
    task's forks, so a task with no fork left pays for no source."""
    if bucket not in SHARES:
        raise ValueError(f"unknown bucket {bucket!r}; choose from {', '.join(SHARES)}")
    run_pairs = pairs(arms, operators)
    if not run_pairs:
        raise ValueError("no (arm, operator) pair to run: natural pairs only with none")
    # `python -m replay.runner --paired` forks every non-natural pair from a natural source run.
    # A natural pair in the plan is already that source; otherwise it is extra.
    forks = any(a is not Arm.natural for a, _ in run_pairs)
    source_runs = int(forks and paired and (Arm.natural, Operator.none) not in run_pairs)
    kept = [[p for p in run_pairs if (i, *p) not in refused] for i in range(n_tasks)]
    runs = sum(
        len(cells) + (source_runs if any(a is not Arm.natural for a, _ in cells) else 0)
        for cells in kept
    )
    if n_tasks <= 0:
        raise ValueError(f"n_tasks must be > 0, got {n_tasks}")
    if runs == 0:
        raise ValueError("every (task, arm, operator) cell is refused: nothing to run")
    n_pairs = len({p for cells in kept for p in cells})
    bucket_usd = SHARES[bucket] * config.budget.total_usd
    n = runs_per_cell(bucket_usd, cost_per_run, runs, 1)
    stop_usd = config.budget.stop_at_fraction * config.budget.total_usd
    # Every run is priced at `cost_per_run`, which must be the cost of a full run (--from-runs
    # leaves fork records out for that reason): a fork replays only the part after the boundary,
    # so a paired sweep is over-counted a little, never under-counted.
    unit = cost_per_run * runs
    n_fit = _floor((stop_usd - spent_usd) / unit)
    capped = n_fit < n
    n = min(n, n_fit)
    return Plan(
        bucket=bucket,
        bucket_usd=bucket_usd,
        cost_per_run=cost_per_run,
        pairs=n_pairs,
        source_runs=source_runs,
        n_tasks=n_tasks,
        runs=runs,
        n=n,
        planned_usd=n * unit,
        spent_usd=spent_usd,
        stop_usd=stop_usd,
        capped=capped,
    )


def mean_cost(records: Iterable[RunRecord]) -> float:
    costs = [r.cost_usd for r in records]
    if not costs:
        raise ValueError("no run records to average")
    return sum(costs) / len(costs)


def _positive(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {text}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m replay.budget", description=__doc__)
    parser.add_argument("--bucket", required=True, choices=list(SHARES))
    parser.add_argument("--arms", required=True, nargs="+", type=Arm, choices=list(Arm))
    parser.add_argument(
        "--operators", required=True, nargs="+", type=Operator, choices=list(Operator)
    )
    parser.add_argument(
        "--tasks",
        required=True,
        nargs="+",
        help="task ids; the cells the runner would refuse for them are listed and not counted",
    )
    parser.add_argument(
        "--paired",
        action="store_true",
        help="count runs as `python -m replay.runner --paired` makes them: the arms other than "
        "natural are forks of a natural source run, and that run is paid for",
    )
    cost = parser.add_mutually_exclusive_group(required=True)
    cost.add_argument("--cost-per-run", type=_positive, help="USD per run")
    cost.add_argument(
        "--from-runs",
        type=Path,
        help="dir whose */record.json give the mean cost; fork records (a fork.json beside them) "
        "are left out, as they cost only the part after the boundary",
    )
    args = parser.parse_args(argv)

    try:
        if args.from_runs is not None:
            records = [
                RunRecord.model_validate_json(p.read_text(encoding="utf-8"))
                for p in sorted(args.from_runs.glob("*/record.json"))
                if not (p.parent / "fork.json").exists()
            ]
            cost_per_run = mean_cost(records)
        else:
            cost_per_run = args.cost_per_run
        if not cost_per_run > 0:
            raise ValueError(f"mean cost per run is {cost_per_run}; need > 0")
        refused = default_refusals(args.tasks, args.arms, args.operators, paired=args.paired)
        for task_id, arm, operator, why in refused:
            print(f"refused: {task_id} {arm.value} {operator.value}: {why}")
        p = plan(
            bucket=args.bucket,
            cost_per_run=cost_per_run,
            arms=args.arms,
            operators=args.operators,
            n_tasks=len(args.tasks),
            config=load_config(_CONFIG_PATH),
            spent_usd=total_spent(),
            paired=args.paired,
            refused={
                (i, arm, operator)
                for i, task_id in enumerate(args.tasks)
                for refused_id, arm, operator, _ in refused
                if refused_id == task_id
            },
        )
    except ValueError as exc:
        parser.error(str(exc))

    print(f"bucket: {p.bucket}")
    print(f"bucket usd: {p.bucket_usd:.2f}")
    print(f"cost per run usd: {p.cost_per_run:.4f}")
    print(f"pairs: {p.pairs}")
    print(f"extra natural source runs per repetition: {p.source_runs}")
    print(f"tasks: {p.n_tasks}")
    print(f"runs per repetition: {p.runs}")
    print(f"spent usd: {p.spent_usd:.2f}")
    print(f"stop line usd: {p.stop_usd:.2f}")
    print(f"planned usd: {p.planned_usd:.2f}")
    print(f"capped: {'yes' if p.capped else 'no'}")
    print(f"N per arm per task: {p.n}")
    return 0 if p.n >= 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
