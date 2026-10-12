"""How many runs per arm per task a budget bucket pays for, capped by the spend stop line.

`python -m replay.budget` prints the plan and makes no model call and no sandbox call.
"""

import argparse
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from common.spend import total_spent
from replay.app import pairs
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
    pairs: int  # number of (arm, operator) pairs per task
    n_tasks: int
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
) -> Plan:
    if bucket not in SHARES:
        raise ValueError(f"unknown bucket {bucket!r}; choose from {', '.join(SHARES)}")
    n_pairs = len(pairs(arms, operators))
    if n_pairs == 0:
        raise ValueError("no (arm, operator) pair to run: natural pairs only with none")
    bucket_usd = SHARES[bucket] * config.budget.total_usd
    n = runs_per_cell(bucket_usd, cost_per_run, n_pairs, n_tasks)
    stop_usd = config.budget.stop_at_fraction * config.budget.total_usd
    unit = cost_per_run * n_pairs * n_tasks
    n_fit = _floor((stop_usd - spent_usd) / unit)
    capped = n_fit < n
    n = min(n, n_fit)
    return Plan(
        bucket=bucket,
        bucket_usd=bucket_usd,
        cost_per_run=cost_per_run,
        pairs=n_pairs,
        n_tasks=n_tasks,
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
    parser.add_argument("--tasks", required=True, nargs="+", help="task ids; only counted")
    cost = parser.add_mutually_exclusive_group(required=True)
    cost.add_argument("--cost-per-run", type=_positive, help="USD per run")
    cost.add_argument("--from-runs", type=Path, help="dir whose */record.json give the mean cost")
    args = parser.parse_args(argv)

    try:
        if args.from_runs is not None:
            records = [
                RunRecord.model_validate_json(p.read_text(encoding="utf-8"))
                for p in sorted(args.from_runs.glob("*/record.json"))
            ]
            cost_per_run = mean_cost(records)
        else:
            cost_per_run = args.cost_per_run
        if not cost_per_run > 0:
            raise ValueError(f"mean cost per run is {cost_per_run}; need > 0")
        p = plan(
            bucket=args.bucket,
            cost_per_run=cost_per_run,
            arms=args.arms,
            operators=args.operators,
            n_tasks=len(args.tasks),
            config=load_config(_CONFIG_PATH),
            spent_usd=total_spent(),
        )
    except ValueError as exc:
        parser.error(str(exc))

    print(f"bucket: {p.bucket}")
    print(f"bucket usd: {p.bucket_usd:.2f}")
    print(f"cost per run usd: {p.cost_per_run:.4f}")
    print(f"pairs: {p.pairs}")
    print(f"tasks: {p.n_tasks}")
    print(f"spent usd: {p.spent_usd:.2f}")
    print(f"stop line usd: {p.stop_usd:.2f}")
    print(f"planned usd: {p.planned_usd:.2f}")
    print(f"capped: {'yes' if p.capped else 'no'}")
    print(f"N per arm per task: {p.n}")
    return 0 if p.n >= 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
