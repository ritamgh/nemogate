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
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from replay.carriage import Label
from replay.noop import is_noop_error, text_noop
from schemas.enums import Arm
from schemas.ledger import Ledger, load_ledger_yaml
from schemas.run import RunRecord, is_comparable

FORMAT_VERSION = 1
Z = 1.96  # 95% two-sided
DECIMALS = 4
UNLABELLED = "unlabelled"  # key for a violating natural run with no carriage.json
KNOB_NAMES = ("NEMOGATE_COMPACT_TOKENS", "NEMOGATE_COMPACT_KEEP")  # as replay.runner records them
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
    # carriage.json "knobs" (compaction tokens, keep), values as recorded, None = unset; the
    # whole field is None when the run does not say (no carriage.json, or one without "knobs")
    knobs: tuple[str | None, str | None] | None = None

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
        fork_source = label = knobs = None
        if (folder / "fork.json").exists():
            fork_source = json.loads((folder / "fork.json").read_bytes())["source_run_id"]
            if not isinstance(fork_source, str) or not fork_source.strip():
                return None
        if (folder / "carriage.json").exists():
            carriage = json.loads((folder / "carriage.json").read_bytes())
            label = Label(carriage["label"])
            if isinstance(carriage.get("knobs"), dict):
                knobs = tuple(carriage["knobs"].get(name) for name in KNOB_NAMES)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return LoadedRun(record, label=label, fork_source=fork_source, knobs=knobs)


def _is_noop_error(folder: Path) -> bool:
    """True if `folder/error.json` says the gate had nothing to mutate (replay/noop.py)."""
    try:
        return is_noop_error(json.loads((folder / "error.json").read_bytes())["error_type"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


class LoadedRuns(NamedTuple):
    """What `load_runs` found. Read the fields by name: a new one must not break callers."""

    runs: list[LoadedRun]  # readable runs, without the mutation no-ops
    unreadable: int  # run folders whose files are missing or invalid
    errored: int  # run folders with error.json and no record.json, not a no-op
    noop: int  # mutation no-ops


def load_runs(paths: Iterable[Path | str]) -> LoadedRuns:
    """The readable runs and the counts of the folders that are not (`LoadedRuns`).

    A folder with error.json and no record.json is an errored run (the runner caught the
    exception and carried on); it is not "unreadable". If that error is the gate's
    NothingToMutate it is a no-op instead, and so is a finished run whose operator left its
    target handoff as it was (`replay.noop.text_noop`); neither is in the runs returned.
    FileNotFoundError if a path is not a directory.
    """
    runs: list[LoadedRun] = []
    unreadable = errored = noop = 0
    for folder in _run_dirs(paths):
        if not (folder / "record.json").exists() and (folder / "error.json").is_file():
            if _is_noop_error(folder):
                noop += 1
            else:
                errored += 1
            continue
        run = _read_run(folder)
        if run is None:
            unreadable += 1
        elif text_noop(folder, run.record):
            noop += 1
        else:
            runs.append(run)
    return LoadedRuns(runs, unreadable, errored, noop)


# ---------------------------------------------------------------- aggregation


def is_exercised(run: LoadedRun) -> bool:
    """False for a run whose carriage label says the rule never came up. It is still counted in
    every cell (the label is given only to runs that did not break the rule, so leaving them out
    would inflate the violation rate), but it is not a run in which the rule was kept, so the
    report's trace panel does not show it as an example of that."""
    return run.label != Label.NOT_EXERCISED


def _pooled_group(runs: Sequence[LoadedRun]) -> list[LoadedRun]:
    """The largest comparable group (`is_comparable`), empty if there are no runs.

    Largest = most runs; ties: the smallest repo_sha, model_ids, safe_config_hash, ledger_hash
    and lockfile_hash in turn (so the choice never depends on input order).
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
        head = group[0].record
        ids = sorted((str(role), name) for role, name in head.model_ids.items())
        return (
            -len(group),
            head.repo_sha,
            ids,
            head.safe_config_hash,
            head.ledger_hash,
            head.lockfile_hash,
        )

    return min(groups, key=rank, default=[])


def _compaction(knobs: tuple[str | None, str | None] | None) -> dict[str, str | None] | None:
    """A cell's compaction field: the knobs as recorded, None when the runs do not say."""
    return None if knobs is None else {"tokens": knobs[0], "keep": knobs[1]}


def _knob_rank(knobs: tuple[str | None, str | None] | None) -> tuple[Any, ...]:
    """Cell order within an (arm, operator): compaction off, then unknown, then on."""
    if knobs is None:
        return (1, "", "")
    off = knobs == (None, None)
    return (0 if off else 2, knobs[0] or "", knobs[1] or "")


def _cell(
    arm: str,
    operator: str,
    knobs: tuple[str | None, str | None] | None,
    runs: Sequence[LoadedRun],
) -> dict[str, Any]:
    n = len(runs)
    n_sources = len({run.source_id for run in runs})
    violations = sum(run.record.constraint_id in run.record.violations for run in runs)
    successes = sum(run.record.task_success for run in runs)
    out = {
        "arm": arm,
        "operator": operator,
        "compaction": _compaction(knobs),
        "n": n,
        "n_sources": n_sources,
        "clustered": n_sources < n,
        "violations": violations,
        "cvr": _rate(violations, n),
        "successes": successes,
        "success": _rate(successes, n),
        "cost_usd": _round(math.fsum(run.record.cost_usd for run in runs)),
    }
    if arm == Arm.natural:
        # Only without sabotage can the label say why a rule broke: lost in the handoff
        # (DROPPED) or carried and broken anyway (DISOBEYED).
        by_label = Counter(
            run.label or UNLABELLED
            for run in runs
            if run.record.constraint_id in run.record.violations
        )
        out["violations_by_label"] = dict(sorted(by_label.items()))
        # Runs where nothing named the guarded thing: they stay in n (they are natural runs of
        # this task), and only the runs without a violation can have this label.
        out["not_exercised"] = sum(run.label == Label.NOT_EXERCISED for run in runs)
    return out


def _comparison(
    name: str, metric: str, key: str, a: Mapping[str, Any], b: Mapping[str, Any]
) -> dict[str, Any]:
    diff, lo, hi = newcombe_diff(a[key], a["n"], b[key], b["n"])
    return {
        "name": name,
        "operator": a["operator"],
        "compaction": a["compaction"],
        "a": {"arm": a["arm"], "operator": a["operator"]},
        "b": {"arm": b["arm"], "operator": b["operator"]},
        "metric": metric,
        "diff": _round(diff),
        "lo": _round(lo),
        "hi": _round(hi),
    }


def _knob_key(cell: Mapping[str, Any]) -> tuple[str | None, str | None] | None:
    knobs = cell["compaction"]
    return None if knobs is None else (knobs["tokens"], knobs["keep"])


def _comparison_order(cell: Mapping[str, Any]) -> tuple[Any, ...]:
    return cell["operator"], _knob_rank(_knob_key(cell))


def _comparisons(cells: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """load_bearing (mutated vs natural) then ledger_value (gated vs block_only), by operator."""
    # A comparison pairs cells with the same compaction knobs: runs with the summariser on and
    # off are not the same condition.
    by_key = {(c["arm"], c["operator"], _knob_key(c)): c for c in cells}
    out: list[dict[str, Any]] = []
    for cell in sorted((c for c in cells if c["arm"] == "mutated"), key=_comparison_order):
        natural = by_key.get(("natural", "none", _knob_key(cell)))
        if natural is not None:
            out.append(_comparison("load_bearing", "cvr", "violations", cell, natural))
    for cell in sorted((c for c in cells if c["arm"] == "gated"), key=_comparison_order):
        partner = by_key.get(("block_only", cell["operator"], _knob_key(cell)))
        if partner is not None:
            out.append(_comparison("ledger_value", "success", "successes", cell, partner))
    return out


def aggregate(
    runs: Sequence[LoadedRun],
    *,
    unreadable: int,
    errored: int = 0,
    noop: int = 0,
    ledger: Ledger,
    extraction_recall: float | None,
) -> dict[str, Any]:
    """The results dict (format_version 1) for `runs` and the `unreadable`, `errored` and `noop`
    (mutation no-op) folders."""
    group = _pooled_group(runs)
    # NOT_EXERCISED runs stay: the label is given only to runs that did not break the rule, so
    # dropping them would inflate the violation rate (see the natural cell's `not_exercised`).
    pooled = sorted(group, key=lambda r: r.record.run_id)
    inconclusive = {
        "unreadable_record": unreadable,
        "run_error": errored,
        "mutation_noop": noop,
        "not_comparable": len(runs) - len(group),
    }
    declared = {c.id: c for c in ledger.constraints}

    tree: dict[str, dict[str, dict[tuple[str, str, Any], list[LoadedRun]]]] = {}
    for run in pooled:
        record = run.record
        if record.constraint_id not in declared:
            raise ValueError(
                f"run {record.run_id} is for constraint {record.constraint_id}, "
                "which the ledger passed with --ledger does not declare"
            )
        cell_runs = tree.setdefault(record.constraint_id, {}).setdefault(record.boundary, {})
        key = (record.arm.value, record.operator.value, run.knobs)
        cell_runs.setdefault(key, []).append(run)

    constraints: dict[str, Any] = {}
    for constraint_id, boundaries in tree.items():
        constraint = declared[constraint_id]
        boundary_out: dict[str, Any] = {}
        for boundary, cell_runs in boundaries.items():
            order = sorted(
                cell_runs, key=lambda key: (ARM_ORDER[key[0]], key[1], _knob_rank(key[2]))
            )
            cells = [_cell(*key, cell_runs[key]) for key in order]
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
            "found": len(runs) + unreadable + errored + noop,
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
        loaded = load_runs(args.paths)
        results = aggregate(
            loaded.runs,
            unreadable=loaded.unreadable,
            errored=loaded.errored,
            noop=loaded.noop,
            ledger=ledger,
            extraction_recall=recall,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # FileNotFoundError, bad recall, bad ledger (YAML or schema), unknown constraint id
        print(f"stats: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    if not any(loaded):
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
