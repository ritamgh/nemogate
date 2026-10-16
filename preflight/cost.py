"""Cost probe, offline: cost per run by arm, from run folders' `record.json`.

`python -m preflight.cost RUN_DIR...` makes no model call and no sandbox call. Each RUN_DIR is a
run folder (holds `record.json`) or a folder of run folders (`*/record.json`, like
`python -m replay.budget --from-runs`). The "all" row's mean is the number that command takes.
"""

import argparse
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from schemas.enums import Arm
from schemas.run import RunRecord


@dataclass(frozen=True)
class CostStats:
    n: int
    mean: float
    median: float
    max: float
    total: float


def _stats(costs: Sequence[float]) -> CostStats:
    return CostStats(
        n=len(costs),
        mean=statistics.fmean(costs),
        median=statistics.median(costs),
        max=max(costs),
        total=sum(costs),
    )


def summarize(records: Iterable[RunRecord]) -> dict[str, CostStats]:
    """Stats per arm that has records (in `Arm` order), then "all"."""
    records = list(records)
    if not records:
        raise ValueError("no run records to summarize")
    out = {}
    for arm in Arm:
        costs = [r.cost_usd for r in records if r.arm == arm]
        if costs:
            out[arm.value] = _stats(costs)
    out["all"] = _stats([r.cost_usd for r in records])
    return out


def record_paths(run_dir: Path) -> list[Path]:
    direct = run_dir / "record.json"
    return [direct] if direct.is_file() else sorted(run_dir.glob("*/record.json"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m preflight.cost", description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path, metavar="RUN_DIR")
    args = parser.parse_args(argv)

    records = []
    for run_dir in args.run_dirs:
        paths = record_paths(run_dir)
        if not paths:
            parser.error(f"no record.json in {run_dir} or in its subfolders")
        for path in paths:
            try:
                records.append(RunRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except ValidationError as exc:
                parser.error(f"{path} is not a valid run record ({exc.error_count()} errors)")

    print(f"{'arm':<12} {'n':>4} {'mean':>9} {'median':>9} {'max':>9} {'total':>9}   (USD per run)")
    for name, s in summarize(records).items():
        print(f"{name:<12} {s.n:>4} {s.mean:>9.4f} {s.median:>9.4f} {s.max:>9.4f} {s.total:>9.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
