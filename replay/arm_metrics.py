"""Per-cell numbers the stats card adds to every cell: cost, wall time, attempts, blocks.

`cell_metrics(facts)` takes one `RunFacts` per run in a cell (an arm and an operator) and
returns the `arm_metrics` dict of the results file (replay/README.md, "stats.py"). Means carry a
95% percentile-bootstrap interval (`replay.intervals.bootstrap_mean`, seeded) once there are
`MIN_FOR_BOOTSTRAP` values; fewer than that, the bootstrap only returns the lowest and highest
value, so `lo` and `hi` are the observed minimum and maximum and `basis` says "range".
Proportions carry a 95% Wilson interval. No file, no clock, no model.

Wall time is the `timing.json` the runner writes; a run without one counts under `unknown`
(trace events carry no timestamps, so there is nothing to derive it from). The recovery cost
from the first block to the end is split pro rata by trace events: the trace has no per-event
cost or time, so `cost_usd_est` and `seconds_est` are the run's total times the share of its
events that follow the first block, an estimate and named as one.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from replay.blocks import RunBlocks
from replay.intervals import MIN_FOR_BOOTSTRAP, bootstrap_mean, rate_stat, round4


@dataclass(frozen=True)
class RunFacts:
    task_success: bool
    cost_usd: float
    wall_seconds: float | None  # None: the run has no timing.json
    blocks: RunBlocks | None  # None: the run has no readable trace


def mean_stat(values: Sequence[float]) -> dict[str, Any]:
    """{"n", "mean", "lo", "hi", "basis"}: mean and interval are None for no values, the interval
    for one. `basis` is "bootstrap" (95%) from `MIN_FOR_BOOTSTRAP` values, "range" (lowest to
    highest value, not an interval estimate) for 2 to that many less one, None otherwise."""
    if not values:
        return {"n": 0, "mean": None, "lo": None, "hi": None, "basis": None}
    mean, lo, hi = bootstrap_mean(values)
    basis = None if lo is None else "bootstrap"
    if lo is not None and len(values) < MIN_FOR_BOOTSTRAP:
        lo, hi, basis = min(values), max(values), "range"
    return {
        "n": len(values),
        "mean": round4(mean),
        "lo": None if lo is None else round4(lo),
        "hi": None if hi is None else round4(hi),
        "basis": basis,
    }


def count_stat(k: int, n: int) -> dict[str, Any] | None:
    """{"k", "n", "rate", "lo", "hi"} (Wilson), or None when there is nothing to divide by."""
    return None if n < 1 else {"k": k, "n": n, **rate_stat(k, n)}


def cell_metrics(facts: Sequence[RunFacts]) -> dict[str, Any]:
    walls = [f.wall_seconds for f in facts if f.wall_seconds is not None]
    traced = [f.blocks for f in facts if f.blocks is not None]
    out: dict[str, Any] = {
        "cost": mean_stat([f.cost_usd for f in facts]),
        "wall_seconds": {**mean_stat(walls), "unknown": len(facts) - len(walls)},
        "attempt_rate": None,
        "blocks": None,
    }
    if not traced:
        return out
    if traced[0].attempts is not None:
        out["attempt_rate"] = count_stat(sum(b.attempts > 0 for b in traced), len(traced))
    out["blocks"] = _blocks(facts, traced)
    return out


def _blocks(facts: Sequence[RunFacts], traced: Sequence[RunBlocks]) -> dict[str, Any]:
    with_block = [f for f in facts if f.blocks is not None and f.blocks.blocked > 0]
    has_attempts = traced[0].attempts is not None
    # carry-over and re-plans need each call tied to its delegation; a run where that was
    # ambiguous (several task calls in one turn) has None there and is left out, and counted
    placed = [b for b in traced if b.carry_over is not None and b.replan_chances is not None]
    recovery = [(f, f.blocks.steps_after_first_block / f.blocks.total_steps) for f in with_block]
    return {
        "n_traced": len(traced),
        "runs_with_block": len(with_block),
        "blocked": mean_stat([b.blocked for b in traced]),
        "repeats": mean_stat([b.repeats for b in traced]),
        "n_ambiguous": sum(b.ambiguous for b in traced),
        "carry_over": mean_stat([b.carry_over for b in placed]) if has_attempts else None,
        "replan": count_stat(
            sum(b.replans for b in placed) if has_attempts else 0,
            sum(b.replan_chances for b in placed) if has_attempts else 0,
        ),
        "stuck": count_stat(sum(not f.task_success for f in with_block), len(with_block)),
        "recovery": None
        if not with_block
        else {
            "n": len(with_block),
            "steps": mean_stat([f.blocks.steps_after_first_block for f in with_block]),
            "cost_usd_est": mean_stat([f.cost_usd * share for f, share in recovery]),
            "seconds_est": mean_stat(
                [f.wall_seconds * share for f, share in recovery if f.wall_seconds is not None]
            ),
        },
    }


def errored_cost(errored: Sequence[tuple[str, float | None]]) -> dict[str, Any]:
    """Money spent on runs that raised and were never graded, in all and per arm.

    `errored` is one (arm, cost or None) per run; None = the cost is not known (an old folder
    whose error.json has no cost). Unknown costs are counted, never added in as 0.
    """

    def total(rows: Sequence[tuple[str, float | None]]) -> dict[str, Any]:
        known = [cost for _, cost in rows if cost is not None]
        return {
            "count": len(rows),
            "cost_usd": round4(math.fsum(known)),
            "cost_unknown": len(rows) - len(known),
        }

    arms = sorted({arm for arm, _ in errored})
    return {
        **total(errored),
        "by_arm": {arm: total([r for r in errored if r[0] == arm]) for arm in arms},
    }
