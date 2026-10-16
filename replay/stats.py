"""Stats card: pool finished runs into cells, put intervals on them and write results.json.

`python -m replay.stats RUNS... --ledger nemogate.yaml [--extraction-recall FILE] --out FILE`
reads run folders (record.json, and fork.json / carriage.json when present; a folder with
error.json and no record.json is an errored run) and writes the
results file the report page reads. The format is in replay/README.md, section "stats.py".
No model call, no sandbox, no network. Loading (`load_runs`) is separate from the arithmetic
(`aggregate`), which takes plain objects and touches no file.
"""

import argparse
import json
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from replay.carriage import Label
from schemas.enums import Arm
from schemas.ledger import Ledger, load_ledger_yaml
from schemas.run import RunRecord, is_comparable

FORMAT_VERSION = 1
Z = 1.96  # 95% two-sided
DECIMALS = 4
ARM_ORDER = {arm.value: i for i, arm in enumerate(Arm)}  # natural, mutated, block_only, gated


def wilson(k: int, n: int, z: float = Z) -> tuple[float, float]:
    """Wilson score interval (lo, hi) for k successes in n trials, clamped to [0, 1]."""
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")
    if not 0 <= k <= n:
        raise ValueError(f"k must be between 0 and n={n}, got {k}")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def newcombe_diff(k1: int, n1: int, k2: int, n2: int, z: float = Z) -> tuple[float, float, float]:
    """(p1 - p2, lo, hi): Newcombe's method 10 (hybrid score) from the two Wilson intervals."""
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    diff = p1 - p2
    lo = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return diff, lo, hi


def _round(value: float) -> float:
    return round(value, DECIMALS) + 0.0  # + 0.0 turns -0.0 into 0.0


def _rate(k: int, n: int) -> dict[str, float]:
    lo, hi = wilson(k, n)
    return {"rate": _round(k / n), "lo": _round(lo), "hi": _round(hi)}


@dataclass(frozen=True)
class LoadedRun:
    """One readable run folder: its record, the carriage label if any, the fork source if any."""

    record: RunRecord
    label: str | None = None  # carriage.json "label"; None if the folder has no carriage.json
    fork_source: str | None = None  # fork.json "source_run_id"; None if the run is not a fork

    @property
    def source_id(self) -> str:
        """The run this one is a sample of: a fork counts under its source, any other as itself."""
        return self.fork_source or self.record.run_id


# ---------------------------------------------------------------- loading


def _is_run_dir(path: Path) -> bool:
    return any((path / name).is_file() for name in ("run_config.json", "record.json", "error.json"))


def _run_dirs(paths: Iterable[Path | str]) -> list[Path]:
    """Run folders among `paths`: each is a run folder, or a parent whose subfolders are.

    A folder given twice, or both directly and through its parent, is listed once.
    """
    found: dict[Path, Path] = {}
    for given in paths:
        path = Path(given)
        if not path.is_dir():
            raise FileNotFoundError(f"not a directory: {path}")
        children = [path] if _is_run_dir(path) else sorted(p for p in path.iterdir() if p.is_dir())
        for child in children:
            if _is_run_dir(child):
                found.setdefault(child.resolve(), child)
    return [found[key] for key in sorted(found)]


def _read_run(folder: Path) -> LoadedRun | None:
    """The run in `folder`, or None if record.json, fork.json or carriage.json is unusable."""
    try:
        record = RunRecord.model_validate_json((folder / "record.json").read_bytes())
        fork_source = label = None
        if (folder / "fork.json").exists():
            fork_source = json.loads((folder / "fork.json").read_bytes())["source_run_id"]
            if not isinstance(fork_source, str) or not fork_source.strip():
                return None
        if (folder / "carriage.json").exists():
            label = Label(json.loads((folder / "carriage.json").read_bytes())["label"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return LoadedRun(record, label=label, fork_source=fork_source)


def load_runs(paths: Iterable[Path | str]) -> tuple[list[LoadedRun], int, int]:
    """(readable runs, run folders whose files are missing or invalid, errored run folders).

    A folder with error.json and no record.json is an errored run (the runner caught the
    exception and carried on); it is not "unreadable". FileNotFoundError if a path is not a
    directory.
    """
    runs: list[LoadedRun] = []
    unreadable = errored = 0
    for folder in _run_dirs(paths):
        if not (folder / "record.json").exists() and (folder / "error.json").is_file():
            errored += 1
            continue
        run = _read_run(folder)
        if run is None:
            unreadable += 1
        else:
            runs.append(run)
    return runs, unreadable, errored


# ---------------------------------------------------------------- aggregation


def _pooled_group(runs: Sequence[LoadedRun]) -> list[LoadedRun]:
    """The largest comparable group (`is_comparable`), empty if there are no runs.

    Largest = most runs that are not NOT_EXERCISED; ties: most runs in all, then the smallest
    repo_sha, model_ids, safe_config_hash, ledger_hash and lockfile_hash in turn (so the choice
    never depends on input order).
    """
    groups: list[list[LoadedRun]] = []
    for run in runs:
        for group in groups:
            if is_comparable(group[0].record, run.record):
                group.append(run)
                break
        else:
            groups.append([run])

    def rank(group: list[LoadedRun]) -> tuple[Any, ...]:
        exercised = sum(run.label != Label.NOT_EXERCISED for run in group)
        head = group[0].record
        ids = sorted((str(role), name) for role, name in head.model_ids.items())
        return (
            -exercised,
            -len(group),
            head.repo_sha,
            ids,
            head.safe_config_hash,
            head.ledger_hash,
            head.lockfile_hash,
        )

    return min(groups, key=rank, default=[])


def _cell(arm: str, operator: str, runs: Sequence[LoadedRun]) -> dict[str, Any]:
    n = len(runs)
    n_sources = len({run.source_id for run in runs})
    violations = sum(run.record.constraint_id in run.record.violations for run in runs)
    successes = sum(run.record.task_success for run in runs)
    return {
        "arm": arm,
        "operator": operator,
        "n": n,
        "n_sources": n_sources,
        "clustered": n_sources < n,
        "violations": violations,
        "cvr": _rate(violations, n),
        "successes": successes,
        "success": _rate(successes, n),
        "cost_usd": _round(math.fsum(run.record.cost_usd for run in runs)),
    }


def _comparison(
    name: str, metric: str, key: str, a: Mapping[str, Any], b: Mapping[str, Any]
) -> dict[str, Any]:
    diff, lo, hi = newcombe_diff(a[key], a["n"], b[key], b["n"])
    return {
        "name": name,
        "operator": a["operator"],
        "a": {"arm": a["arm"], "operator": a["operator"]},
        "b": {"arm": b["arm"], "operator": b["operator"]},
        "metric": metric,
        "diff": _round(diff),
        "lo": _round(lo),
        "hi": _round(hi),
    }


def _comparisons(cells: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """load_bearing (mutated vs natural) then ledger_value (gated vs block_only), by operator."""
    by_key = {(c["arm"], c["operator"]): c for c in cells}
    natural = by_key.get(("natural", "none"))
    out: list[dict[str, Any]] = []
    for cell in sorted((c for c in cells if c["arm"] == "mutated"), key=lambda c: c["operator"]):
        if natural is not None:
            out.append(_comparison("load_bearing", "cvr", "violations", cell, natural))
    for cell in sorted((c for c in cells if c["arm"] == "gated"), key=lambda c: c["operator"]):
        partner = by_key.get(("block_only", cell["operator"]))
        if partner is not None:
            out.append(_comparison("ledger_value", "success", "successes", cell, partner))
    return out


def aggregate(
    runs: Sequence[LoadedRun],
    *,
    unreadable: int,
    errored: int = 0,
    ledger: Ledger,
    extraction_recall: float | None,
) -> dict[str, Any]:
    """The results dict (format_version 1) for `runs` and the `unreadable` and `errored` folders."""
    group = _pooled_group(runs)
    pooled = sorted(
        (run for run in group if run.label != Label.NOT_EXERCISED), key=lambda r: r.record.run_id
    )
    inconclusive = {
        "unreadable_record": unreadable,
        "run_error": errored,
        "not_comparable": len(runs) - len(group),
        "not_exercised": len(group) - len(pooled),
    }
    declared = {c.id: c for c in ledger.constraints}

    tree: dict[str, dict[str, dict[tuple[str, str], list[LoadedRun]]]] = {}
    for run in pooled:
        record = run.record
        if record.constraint_id not in declared:
            raise ValueError(
                f"run {record.run_id} is for constraint {record.constraint_id}, "
                "which the ledger passed with --ledger does not declare"
            )
        cell_runs = tree.setdefault(record.constraint_id, {}).setdefault(record.boundary, {})
        cell_runs.setdefault((record.arm.value, record.operator.value), []).append(run)

    constraints: dict[str, Any] = {}
    for constraint_id, boundaries in tree.items():
        constraint = declared[constraint_id]
        boundary_out: dict[str, Any] = {}
        for boundary, cell_runs in boundaries.items():
            order = sorted(cell_runs, key=lambda key: (ARM_ORDER[key[0]], key[1]))
            cells = [_cell(arm, operator, cell_runs[(arm, operator)]) for arm, operator in order]
            boundary_out[boundary] = {"cells": cells, "comparisons": _comparisons(cells)}
        constraints[constraint_id] = {
            "text": constraint.text,
            "kind": constraint.kind.value,
            "has_check": constraint.check is not None,
            "task_ids": sorted(
                {r.record.task_id for cs in boundaries.values() for rs in cs.values() for r in rs}
            ),
            "boundaries": boundary_out,
        }

    total = len(ledger.constraints)
    with_check = sum(c.check is not None for c in ledger.constraints)
    head = pooled[0].record if pooled else None
    return {
        "format_version": FORMAT_VERSION,
        "reproducibility": None
        if head is None
        else {
            "model_ids": {str(role): name for role, name in head.model_ids.items()},
            "safe_config_hash": head.safe_config_hash,
            "ledger_hash": head.ledger_hash,
            "repo_sha": head.repo_sha,
            "lockfile_hash": head.lockfile_hash,
        },
        "runs": {
            "found": len(runs) + unreadable + errored,
            "pooled": len(pooled),
            "inconclusive": inconclusive,
        },
        "honesty": {
            "constraints_total": total,
            "constraints_with_check": with_check,
            "check_share": _round(with_check / total) if total else None,
            "extraction_recall": None if extraction_recall is None else _round(extraction_recall),
            "inconclusive_total": sum(inconclusive.values()),
        },
        "constraints": constraints,
    }


def render(results: Mapping[str, Any]) -> str:
    """Deterministic JSON text: sorted keys, two-space indent, trailing newline."""
    return json.dumps(results, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------- CLI


def _read_recall(path: Path) -> float:
    try:
        recall = json.loads(path.read_text(encoding="utf-8"))["recall"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f'{path}: expected a JSON object with a numeric "recall": {exc}') from exc
    if isinstance(recall, bool) or not isinstance(recall, int | float) or not 0 <= recall <= 1:
        raise ValueError(f"{path}: recall must be a number in [0, 1], got {recall!r}")
    return float(recall)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m replay.stats", description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="run folders, or folders of runs")
    parser.add_argument("--ledger", type=Path, required=True, help="the ledger the runs used")
    parser.add_argument(
        "--extraction-recall", type=Path, help='JSON file {"recall": 0.83}; absent = not measured'
    )
    parser.add_argument("--out", type=Path, required=True, help="results.json to write")
    args = parser.parse_args(argv)
    try:
        recall = None if args.extraction_recall is None else _read_recall(args.extraction_recall)
        ledger = load_ledger_yaml(args.ledger.read_text(encoding="utf-8"))
        runs, unreadable, errored = load_runs(args.paths)
        results = aggregate(
            runs, unreadable=unreadable, errored=errored, ledger=ledger, extraction_recall=recall
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # FileNotFoundError, bad recall, bad ledger (YAML or schema), unknown constraint id
        print(f"stats: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    if not runs and not unreadable and not errored:
        print("stats: no run folders found; writing an empty result", file=sys.stderr)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(render(results).encode("utf-8"))
    counts = results["runs"]
    print(
        f"wrote {args.out}: found {counts['found']}, pooled {counts['pooled']}, "
        f"inconclusive {results['honesty']['inconclusive_total']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
