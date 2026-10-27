"""Sensitivity check: what the rate of one cell would be if its step-limit runs counted.

`python -m report.step_limit RUN_DIR_OR_PARENT... --ledger nemogate.yaml --task ID
--constraint ID --arm natural --out FILE [--operator none] [--boundary ID] [--repo-sha COMMIT]`

A run that hits the graph's step limit (the `recursion_limit` the runner sets) ends in a
GraphRecursionError: its folder has error.json and a partial trace.jsonl but no record.json, so
`replay.stats` leaves it out of every rate. This writes a two-row markdown table: the cell as
published (finished runs only, loaded and pooled by `replay.stats` itself, so it equals the
results page), and the same cell with each step-limit run of the same pooled conditions counted
as finished. Such a run is counted as task failed, and its rule is judged by `oracle.evaluate`
on the partial trace, with the filesystem diff taken as empty unless the folder has fs_diff.json.
It reads saved folders only and changes no score: no model call, no sandbox, no network.
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from oracle.evaluate import evaluate
from replay import stats
from replay.intervals import wilson
from replay.noop import is_noop_error
from replay.record import scoped_ledger
from schemas.ledger import load_ledger_yaml
from schemas.run import FsChange

STEP_LIMIT_ERROR = "GraphRecursionError"  # class name of the error the step limit raises
REPRO_FIELDS = ("model_ids", "safe_config_hash", "ledger_hash", "repo_sha", "lockfile_hash")
NO_FS_DIFF = "none, taken as empty"

LEFT_OTHER_COMMIT = "step limit, another commit"
LEFT_NO_VERDICT = "step limit, no verdict"
LEFT_FINISHED_UNPOOLED = "finished, not pooled"


@dataclass
class Row:
    label: str
    n: int
    broke: int
    done: int  # finished the task and kept the rule
    left_out: int


@dataclass
class StepLimitRun:
    run_id: str
    task_success: bool = False  # a run stopped by the step limit never got to a passing check
    violated: bool | None = None  # None: no verdict (the trace or ledger could not be read)
    kinds: list[str] = field(default_factory=list)  # "predicate: detail" per violation
    fs_diff: str = NO_FS_DIFF
    pooled: bool = True  # same conditions as the published group


@dataclass
class Report:
    task: str
    constraint: str
    arm: str
    operator: str
    boundary: str
    published: Row
    sensitivity: Row
    step_limit_runs: list[StepLimitRun]
    left_out: Counter[str]  # why runs of the cell are in neither row, or only in the first


def _is_step_limit(error_type: Any) -> bool:
    return isinstance(error_type, str) and error_type.rsplit(".", 1)[-1] == STEP_LIMIT_ERROR


def _judge(folder: Path, constraint: str) -> StepLimitRun:
    """The step-limit run in `folder`, its rule judged on the partial trace by the oracle."""
    run = StepLimitRun(run_id=folder.name)
    try:
        trace = stats._read_trace(folder)
        if trace is None:
            return run
        ledger = scoped_ledger(
            load_ledger_yaml((folder / "ledger.yaml").read_text(encoding="utf-8")), constraint
        )
        diff: list[FsChange] = []
        if (folder / "fs_diff.json").is_file():
            diff = [
                FsChange.model_validate(item)
                for item in json.loads((folder / "fs_diff.json").read_bytes())
            ]
            run.fs_diff = "fs_diff.json"
        violations = evaluate(ledger, list(trace), diff)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        run.fs_diff = NO_FS_DIFF
        return run
    run.violated = bool(violations)
    run.kinds = [f"{v.predicate}: {v.detail}" for v in violations]
    return run


def _same_conditions(error: dict[str, Any], reproducibility: dict[str, Any]) -> bool:
    return all(error.get(name) == reproducibility.get(name) for name in REPRO_FIELDS)


def _cells(results: dict[str, Any], constraint: str, arm: str, operator: str, boundary: str | None):
    """(boundary, cells of that arm and operator) of the rule's pooled group; ValueError if the
    rule has none or several boundaries match and none was named."""
    rule = results["constraints"].get(constraint)
    if rule is None:
        raise ValueError(
            f"no finished run of rule {constraint} was pooled, so there is no published row "
            "and no conditions to count step-limit runs under"
        )
    found = {
        name: [c for c in body["cells"] if c["arm"] == arm and c["operator"] == operator]
        for name, body in rule["boundaries"].items()
        if boundary in (None, name)
    }
    found = {name: cells for name, cells in found.items() if cells}
    if len(found) != 1:
        raise ValueError(
            f"rule {constraint}, arm {arm}, operator {operator}: expected one boundary with "
            f"cells, found {sorted(found) or 'none'}; name one with --boundary"
        )
    ((name, cells),) = found.items()
    return name, cells, rule["reproducibility"]


def build(
    paths: Sequence[Path | str],
    ledger_path: Path,
    task: str,
    constraint: str,
    arm: str,
    *,
    operator: str = "none",
    boundary: str | None = None,
    repo_sha: str | None = None,
) -> Report:
    """The two rows for one cell of the results page. ValueError if the cell has no published
    row; OSError for a missing path or ledger."""
    ledger = load_ledger_yaml(ledger_path.read_text(encoding="utf-8"))
    loaded = stats.load_runs(paths)
    in_rule = [r for r in loaded.runs if r.record.constraint_id == constraint]
    results = stats.aggregate(
        in_rule,
        unreadable=0,
        ledger=ledger,
        extraction_recall=None,
        repo_sha_pins={} if repo_sha is None else {task: repo_sha},
    )
    boundary, cells, reproducibility = _cells(results, constraint, arm, operator, boundary)
    n = sum(c["n"] for c in cells)
    broke = sum(c["outcomes"]["broke"] for c in cells)
    done = sum(c["outcomes"]["done"] for c in cells)

    cell_key = (task, constraint, arm, operator, boundary)
    left_out: Counter[str] = Counter()
    pooled_finished = sum(
        (
            r.record.task_id,
            r.record.constraint_id,
            r.record.arm.value,
            r.record.operator.value,
            r.record.boundary,
        )
        == cell_key
        for r in loaded.runs
    )
    left_out[LEFT_FINISHED_UNPOOLED] = pooled_finished - n
    steps: list[StepLimitRun] = []
    for folder in stats._run_dirs(paths):
        if (folder / "record.json").exists() or not (folder / "error.json").is_file():
            continue
        try:
            error = json.loads((folder / "error.json").read_bytes())
            key = tuple(
                error[name] for name in ("task_id", "constraint_id", "arm", "operator", "boundary")
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        error_type = error.get("error_type")
        if key != cell_key or is_noop_error(error_type):
            continue
        if not _is_step_limit(error_type):
            left_out[f"error: {error_type}"] += 1
            continue
        run = _judge(folder, constraint)
        run.pooled = _same_conditions(error, reproducibility)
        steps.append(run)
        if not run.pooled:
            left_out[LEFT_OTHER_COMMIT] += 1
        elif run.violated is None:
            left_out[LEFT_NO_VERDICT] += 1
    left_out = Counter({reason: count for reason, count in left_out.items() if count})

    counted = [r for r in steps if r.pooled and r.violated is not None]
    out_of_both = sum(left_out.values())
    published = Row("As published (finished runs only)", n, broke, done, out_of_both + len(counted))
    sensitivity = Row(
        "With step-limit runs counted as finished",
        n + len(counted),
        broke + sum(bool(r.violated) for r in counted),
        done,  # task failed for every step-limit run: none is added to "did the job"
        out_of_both,
    )
    return Report(
        task, constraint, arm, operator, boundary, published, sensitivity, steps, left_out
    )


# ---------------------------------------------------------------- markdown


def _share(k: int, n: int) -> str:
    if n < 1:
        return f"{k} of {n}"
    lo, hi = wilson(k, n)
    return f"{k} of {n} ({k / n:.1%}, Wilson 95% {lo:.1%} to {hi:.1%})"


def _cell(text: object) -> str:
    return " ".join(str(text).split("\n")).replace("|", "\\|")


def render(report: Report) -> str:
    """The markdown page: the table, how it was made, and each step-limit run."""
    lines = [
        f"# Step-limit sensitivity: {report.task} {report.constraint}, {report.arm} arm",
        "",
        f"Boundary `{report.boundary}`, operator `{report.operator}`.",
        "",
        "## Sensitivity check, not the published numbers",
        "",
        "A run that reaches the step limit ends in an error with no finished record, so the "
        "published rate leaves it out. The second row counts each such run (same pooled "
        "conditions) as finished: task failed, and the rule judged by the oracle on the partial "
        "trace, with the filesystem diff taken as empty unless the run folder has one. A rule "
        "judged only from a partial trace can miss a violation that came after the cut, so the "
        "second row is a floor on the broken count for these runs, not an exact figure.",
        "",
        "| Row | Runs counted | Broke the rule | Did the job and kept the rule | Left out |",
        "|---|---|---|---|---|",
    ]
    for row in (report.published, report.sensitivity):
        lines.append(
            f"| {_cell(row.label)} | {row.n} | {_share(row.broke, row.n)} "
            f"| {_share(row.done, row.n)} | {row.left_out} |"
        )
    lines += ["", "Left out of both rows:"]
    lines += [f"- {_cell(reason)}: {count}" for reason, count in sorted(report.left_out.items())]
    if not report.left_out:
        lines.append("- nothing")
    counted = sum(r.pooled and r.violated is not None for r in report.step_limit_runs)
    lines += [
        f"- step limit, counted in the second row only: {counted}",
        "",
        "## Step-limit runs",
        "",
        "| Run | Violation | Kind | Filesystem diff | In second row |",
        "|---|---|---|---|---|",
    ]
    for run in report.step_limit_runs:
        verdict = "unknown" if run.violated is None else ("yes" if run.violated else "no")
        kind = "; ".join(run.kinds) or "-"
        in_row = "yes" if run.pooled and run.violated is not None else "no"
        lines.append(
            f"| {_cell(run.run_id)} | {verdict} | {_cell(kind)} | {_cell(run.fs_diff)} | {in_row} |"
        )
    if not report.step_limit_runs:
        lines.append("| none | | | | |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m report.step_limit", description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="run folders, or folders of runs")
    parser.add_argument("--ledger", type=Path, required=True, help="the ledger the runs used")
    parser.add_argument("--task", required=True, help="task id of the cell")
    parser.add_argument("--constraint", required=True, help="rule id of the cell")
    parser.add_argument("--arm", required=True, help="arm of the cell, e.g. natural")
    parser.add_argument("--operator", default="none", help="operator of the cell (default none)")
    parser.add_argument("--boundary", help="boundary of the cell, if the rule has several")
    parser.add_argument(
        "--repo-sha",
        metavar="COMMIT",
        help="pool the task's runs at COMMIT (a repo_sha or a prefix), as replay.stats does",
    )
    parser.add_argument("--out", type=Path, required=True, help="markdown file to write")
    args = parser.parse_args(argv)
    try:
        report = build(
            args.paths,
            args.ledger,
            args.task,
            args.constraint,
            args.arm,
            operator=args.operator,
            boundary=args.boundary,
            repo_sha=args.repo_sha,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"step_limit: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    text = render(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
