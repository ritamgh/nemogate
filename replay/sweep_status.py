"""Sweep accounting: what a runs directory already holds, cell by cell.

`uv run python -m replay.sweep_status --runs-dir DIR [--tasks ID...]` prints one row per
(task, arm, operator, reproducibility group): finished records, errored runs, and among the
records the violations of the target constraint and the task successes. Offline: it reads
files only. The same scan feeds `python -m replay.runner --top-up` (replay/README.md,
"sweep_status.py").

An attempt is a run folder with `record.json` (finished) or `error.json` (the runner caught
its exception and carried on). A folder with neither is an orphan and counts for nothing.
A fork folder (`fork.json`) is not an independent sample and is left out of every count.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from replay.app import pairs
from schemas.enums import Arm, Operator
from schemas.run import RunRecord

REPRODUCIBILITY_FIELDS = (
    "model_ids",
    "safe_config_hash",
    "ledger_hash",
    "repo_sha",
    "lockfile_hash",
)
Cell = tuple[str, str, str]  # (task id, arm, operator), as strings


def group_key(fields: Mapping[str, Any]) -> str:
    """A canonical string of the five reproducibility fields; equal keys = comparable runs."""
    model_ids = {str(role): name for role, name in fields["model_ids"].items()}
    canonical = {name: fields[name] for name in REPRODUCIBILITY_FIELDS if name != "model_ids"}
    return json.dumps({**canonical, "model_ids": model_ids}, sort_keys=True, separators=(",", ":"))


def group_hash(key: str) -> str:
    """The first 8 hex of the sha256 of a group key: the short name a status row shows."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class Attempt:
    run_id: str
    cell: Cell
    group: str  # group_key of the run's reproducibility fields
    errored: bool  # error.json and no record.json
    violated: bool = False  # the record lists its target constraint (records only)
    success: bool = False  # the record's task_success (records only)


@dataclass(frozen=True)
class Scan:
    attempts: tuple[Attempt, ...]
    orphans: tuple[str, ...]  # run folders with neither record.json nor a usable error.json
    forks: int  # fork folders, left out of everything else


def _is_run_folder(folder: Path) -> bool:
    return folder.name.startswith("r-") or any(
        (folder / name).is_file() for name in ("record.json", "error.json", "run_config.json")
    )


def _read_attempt(folder: Path) -> Attempt | None:
    """The attempt in `folder`, or None if record.json (or, without one, error.json) is unusable."""
    try:
        if (folder / "record.json").is_file():
            rec = RunRecord.model_validate_json((folder / "record.json").read_bytes())
            return Attempt(
                folder.name,
                (rec.task_id, rec.arm.value, rec.operator.value),
                group_key(rec.model_dump(mode="json")),
                errored=False,
                violated=rec.constraint_id in rec.violations,
                success=rec.task_success,
            )
        data = json.loads((folder / "error.json").read_bytes())
        cell = (data["task_id"], Arm(data["arm"]).value, Operator(data["operator"]).value)
        if not isinstance(data["task_id"], str) or not isinstance(data["model_ids"], dict):
            return None
        return Attempt(folder.name, cell, group_key(data), errored=True)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def scan(runs_dir: Path) -> Scan:
    """Every run folder directly under `runs_dir` (none if it does not exist), sorted by name."""
    attempts: list[Attempt] = []
    orphans: list[str] = []
    forks = 0
    folders = sorted(p for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.is_dir() else []
    for folder in folders:
        if not _is_run_folder(folder):
            continue
        if (folder / "fork.json").exists():
            forks += 1
            continue
        attempt = _read_attempt(folder)
        if attempt is None:
            orphans.append(folder.name)
        else:
            attempts.append(attempt)
    return Scan(tuple(attempts), tuple(orphans), forks)


@dataclass(frozen=True)
class TopUpCell:
    task_id: str
    arm: str
    operator: str
    existing: int  # attempts (records and errored runs) with this invocation's group
    planned: int  # runs still to do: max(0, n - existing)


def top_up_plan(
    found: Scan,
    reproducibility: Mapping[str, Any],
    task_ids: Sequence[str],
    arms: Sequence[Arm],
    operators: Sequence[Operator],
    n: int,
) -> list[TopUpCell]:
    """Per (task, arm, operator): attempts already made under `reproducibility`, and the rest.

    Errored runs count as attempts: retrying them would bias the sample, because a run that
    failed for a reason tied to its conditions would be replaced by one that did not.
    """
    here = group_key(reproducibility)
    have = Counter(a.cell for a in found.attempts if a.group == here)
    cells = []
    for task_id in task_ids:
        for arm, operator in pairs(arms, operators):
            existing = have[(task_id, arm.value, operator.value)]
            cells.append(
                TopUpCell(task_id, arm.value, operator.value, existing, max(0, n - existing))
            )
    return cells


@dataclass
class _Row:
    records: int = 0
    errored: int = 0
    violations: int = 0
    successes: int = 0


def _arm_order(arm: str) -> int:
    return [a.value for a in Arm].index(arm)


def status_rows(found: Scan, task_ids: Sequence[str] | None = None) -> dict[tuple[str, ...], _Row]:
    """Counts per (task, arm, operator, group hash), sorted by task, arm order, operator, group."""
    rows: dict[tuple[str, ...], _Row] = {}
    for a in found.attempts:
        if task_ids and a.cell[0] not in task_ids:
            continue
        row = rows.setdefault((*a.cell, group_hash(a.group)), _Row())
        if a.errored:
            row.errored += 1
        else:
            row.records += 1
            row.violations += a.violated
            row.successes += a.success
    return dict(sorted(rows.items(), key=lambda kv: (kv[0][0], _arm_order(kv[0][1]), *kv[0][2:])))


def _group_line(key: str) -> str:
    data = json.loads(key)
    models = ",".join(f"{role}={name}" for role, name in sorted(data["model_ids"].items()))
    return (
        f"{group_hash(key)}  repo_sha={data['repo_sha'][:12]} ledger_hash={data['ledger_hash']} "
        f"safe_config_hash={data['safe_config_hash']} lockfile_hash={data['lockfile_hash']} "
        f"models={models}"
    )


def render(found: Scan, task_ids: Sequence[str] | None = None) -> str:
    rows = status_rows(found, task_ids)
    header = ("task", "arm", "operator", "group", "records", "errored", "violations", "successes")
    table = [header]
    for key, row in rows.items():
        table.append(
            (*key, *(str(v) for v in (row.records, row.errored, row.violations, row.successes)))
        )
    widths = [max(len(line[i]) for line in table) for i in range(len(header))]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip()
        for line in table
    ]
    keys = {a.group for a in found.attempts if not task_ids or a.cell[0] in task_ids}
    if keys:
        lines += ["", "groups (the five reproducibility fields):"]
        lines += [f"  {_group_line(key)}" for key in sorted(keys, key=group_hash)]
    lines.append("")
    lines.append(f"orphans (no record.json or error.json): {len(found.orphans)}")
    lines += [f"  {name}" for name in found.orphans]
    lines.append(f"forks (not counted): {found.forks}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m replay.sweep_status", description=__doc__)
    parser.add_argument("--runs-dir", required=True, type=Path, help="the directory of run folders")
    parser.add_argument("--tasks", nargs="+", help="only these task ids")
    args = parser.parse_args(argv)
    if not args.runs_dir.is_dir():
        print(f"sweep_status: not a directory: {args.runs_dir}", file=sys.stderr)
        return 2
    sys.stdout.write(render(scan(args.runs_dir), args.tasks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
