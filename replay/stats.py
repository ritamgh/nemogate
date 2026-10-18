"""Stats card: pool finished runs into cells, put intervals on them and write results.json.

`python -m replay.stats RUNS... --ledger nemogate.yaml [--extraction-recall FILE]
[--repo-sha TASK=COMMIT]... --out FILE`
reads run folders (record.json, and fork.json / carriage.json when present; a folder with
error.json and no record.json is an errored run) and writes the
results file the report page reads. The format is in replay/README.md, section "stats.py".
Each run folder may also have `trace.jsonl` and `timing.json`; they feed the per-arm numbers
(cost, wall time, attempts, blocks) of `replay.arm_metrics`. No model call, no sandbox, no
network. Loading (`load_runs`) is separate from the arithmetic
(`aggregate`), which takes plain objects and touches no file.
"""

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from common.env import compaction_from_env
from common.spend import run_cost
from gate.policy import checkable_before_the_call
from replay.arm_metrics import RunFacts, cell_metrics, errored_cost
from replay.blocks import run_blocks
from replay.carriage import Label
from replay.intervals import Z, rate_stat, round4, wilson
from replay.noop import is_noop_error, text_noop
from schemas.enums import Arm
from schemas.ledger import Ledger, load_ledger_yaml
from schemas.run import RunRecord, is_comparable
from schemas.trace import Event, parse_trace_jsonl

FORMAT_VERSION = 2  # 2: cells gain arm_metrics, runs gains errored_cost
UNLABELLED = "unlabelled"  # key for a violating natural run with no carriage.json
KNOB_NAMES = ("NEMOGATE_COMPACT_TOKENS", "NEMOGATE_COMPACT_KEEP")  # as replay.runner records them
ARM_ORDER = {arm.value: i for i, arm in enumerate(Arm)}  # natural, mutated, block_only, gated


def newcombe_diff(k1: int, n1: int, k2: int, n2: int, z: float = Z) -> tuple[float, float, float]:
    """(p1 - p2, lo, hi): Newcombe's method 10 (hybrid score) from the two Wilson intervals."""
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    diff = p1 - p2
    lo = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return diff, lo, hi


_round = round4  # the helpers moved to replay/intervals.py, where arm_metrics uses them too
_rate = rate_stat


@dataclass(frozen=True)
class LoadedRun:
    """One readable run folder: its record, the carriage label if any, the fork source if any."""

    record: RunRecord
    label: str | None = None  # carriage.json "label"; None if the folder has no carriage.json
    fork_source: str | None = None  # fork.json "source_run_id"; None if the run is not a fork
    # The compaction setting the run was made under, as `compaction_from_env` reads carriage.json
    # "knobs": (tokens, keep), or (None, None) for off. The whole field is None when the run
    # does not say (no carriage.json, or one without "knobs").
    knobs: tuple[int | None, int | None] | None = None
    trace: tuple[Event, ...] | None = None  # trace.jsonl (a fork's own events); None if unreadable
    wall_seconds: float | None = None  # timing.json "seconds"; None for a run made before it
    # carriage.json "classifier_version"; None if the file has none or there is no file
    classifier_version: int | None = None

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


def _effective_knobs(recorded: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """The setting the recorded env strings mean, read the way the app reads them (an empty
    value is unset, keep defaults to 6); ValueError for one the app would have refused."""
    environ = {name: recorded[name] for name in KNOB_NAMES if recorded.get(name) is not None}
    setting = compaction_from_env(environ)
    return (None, None) if setting is None else setting


def _read_run(folder: Path) -> LoadedRun | None:
    """The run in `folder`, or None if record.json, fork.json or carriage.json is unusable."""
    try:
        record = RunRecord.model_validate_json((folder / "record.json").read_bytes())
        fork_source = label = knobs = version = None
        if (folder / "fork.json").exists():
            fork_source = json.loads((folder / "fork.json").read_bytes())["source_run_id"]
            if not isinstance(fork_source, str) or not fork_source.strip():
                return None
        if (folder / "carriage.json").exists():
            carriage = json.loads((folder / "carriage.json").read_bytes())
            label = Label(carriage["label"])
            version = carriage.get("classifier_version")
            if isinstance(carriage.get("knobs"), dict):
                knobs = _effective_knobs(carriage["knobs"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return LoadedRun(
        record,
        label=label,
        fork_source=fork_source,
        knobs=knobs,
        trace=_read_trace(folder),
        wall_seconds=_read_wall_seconds(folder),
        classifier_version=version,
    )


def _read_trace(folder: Path) -> tuple[Event, ...] | None:
    try:
        return tuple(parse_trace_jsonl((folder / "trace.jsonl").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _read_wall_seconds(folder: Path) -> float | None:
    """The run's wall time from timing.json; None if it is missing or not a sane duration."""
    try:
        seconds = json.loads((folder / "timing.json").read_bytes())["seconds"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        return None
    return float(seconds) if math.isfinite(seconds) and seconds >= 0 else None


def _is_noop_error(folder: Path) -> bool:
    """True if `folder/error.json` says the gate had nothing to mutate (replay/noop.py)."""
    try:
        return is_noop_error(json.loads((folder / "error.json").read_bytes())["error_type"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


class ErroredRun(NamedTuple):
    """A run that raised: its arm, and what it cost (None = not recorded and not in the log)."""

    arm: str
    cost_usd: float | None


def _read_errored(folder: Path, spend_log: Path | str | None) -> ErroredRun:
    """The arm and cost in `folder/error.json`; an old file with no cost is looked up in the
    spend log if one is given. No log, or no line in it for this run: the cost is unknown."""
    try:
        data = json.loads((folder / "error.json").read_bytes())
        arm, cost = data.get("arm"), data.get("cost_usd")
    except (OSError, ValueError, AttributeError):
        arm = cost = None
    if isinstance(cost, bool) or not isinstance(cost, int | float):
        cost = run_cost(folder.name, spend_log) if spend_log is not None else None
    return ErroredRun(arm if isinstance(arm, str) else "unknown", cost)


class LoadedRuns(NamedTuple):
    """What `load_runs` found. Read the fields by name: a new one must not break callers."""

    runs: list[LoadedRun]  # readable runs, without the mutation no-ops
    unreadable: int  # run folders whose files are missing or invalid
    errored: int  # run folders with error.json and no record.json, not a no-op
    noop: int  # mutation no-ops
    errored_runs: tuple[ErroredRun, ...] = ()  # the errored ones, with their cost


def load_runs(paths: Iterable[Path | str], spend_log: Path | str | None = None) -> LoadedRuns:
    """The readable runs and the counts of the folders that are not (`LoadedRuns`).

    A folder with error.json and no record.json is an errored run (the runner caught the
    exception and carried on); it is not "unreadable". If that error is the gate's
    NothingToMutate it is a no-op instead, and so is a finished run whose operator left its
    target handoff as it was (`replay.noop.text_noop`); neither is in the runs returned.
    `spend_log` (a spend.jsonl) is only read for an errored run whose error.json has no cost.
    FileNotFoundError if a path is not a directory.
    """
    runs: list[LoadedRun] = []
    errored_runs: list[ErroredRun] = []
    unreadable = errored = noop = 0
    for folder in _run_dirs(paths):
        if not (folder / "record.json").exists() and (folder / "error.json").is_file():
            if _is_noop_error(folder):
                noop += 1
            else:
                errored += 1
                errored_runs.append(_read_errored(folder, spend_log))
            continue
        run = _read_run(folder)
        if run is None:
            unreadable += 1
        elif text_noop(folder, run.record):
            noop += 1
        else:
            runs.append(run)
    return LoadedRuns(runs, unreadable, errored, noop, tuple(errored_runs))


# ---------------------------------------------------------------- aggregation


def is_exercised(run: LoadedRun) -> bool:
    """False for a run whose carriage label says the rule never came up. It is still counted in
    every cell (the label is given only to runs that did not break the rule, so leaving them out
    would inflate the violation rate), but it is not a run in which the rule was kept, so the
    report's trace panel does not show it as an example of that."""
    return run.label != Label.NOT_EXERCISED


def _group_rank(group: Sequence[LoadedRun]) -> tuple[Any, ...]:
    """Sort key of a comparable group: the largest first; ties: the smallest repo_sha,
    model_ids, safe_config_hash, ledger_hash and lockfile_hash in turn (so the choice never
    depends on input order)."""
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


def _pooled_group(runs: Sequence[LoadedRun]) -> list[LoadedRun]:
    """The largest comparable group (`is_comparable`), empty if there are no runs."""
    groups: list[list[LoadedRun]] = []
    for run in runs:
        for group in groups:
            if is_comparable(group[0].record, run.record):
                group.append(run)
                break
        else:
            groups.append([run])
    return min(groups, key=_group_rank, default=[])


def _reproducibility(record: RunRecord) -> dict[str, Any]:
    """The conditions a group of runs shares, as results.json writes them."""
    return {
        "model_ids": {str(role): name for role, name in record.model_ids.items()},
        "safe_config_hash": record.safe_config_hash,
        "ledger_hash": record.ledger_hash,
        "repo_sha": record.repo_sha,
        "lockfile_hash": record.lockfile_hash,
    }


def _pin_filter(runs: Sequence[LoadedRun], pins: Mapping[str, str]) -> Callable[[LoadedRun], bool]:
    """Whether a run may be pooled given `pins` (task id -> repo_sha or a prefix of it): a run
    of a pinned task must be at that commit, every other run is unaffected. ValueError if a pin
    matches no run of its task, so a mistyped pin never silently pools something else (that the
    pinned runs survive the pooling is checked by `_pins_took_effect`)."""
    for task, sha in pins.items():
        if not any(r.record.task_id == task and r.record.repo_sha.startswith(sha) for r in runs):
            raise ValueError(f"--repo-sha {task}={sha}: no run of task {task} is at that commit")

    def allowed(run: LoadedRun) -> bool:
        sha = pins.get(run.record.task_id)
        return sha is None or run.record.repo_sha.startswith(sha)

    return allowed


def _pins_took_effect(
    by_rule: Mapping[str, Sequence[LoadedRun]],
    groups: Mapping[str, Sequence[LoadedRun]],
    pins: Mapping[str, str],
) -> None:
    """ValueError if a pinned task has runs of a rule and none of them is in the rule's pooled
    group: the rule was pooled at another commit (a larger or tie-winning group of another task of
    the rule) or at nothing (the task's runs of it are at other commits only). A pin either takes
    effect or fails loudly, so a task never disappears from a rule behind an exit code of 0."""
    for cid, rule_runs in by_rule.items():
        pooled_tasks = {r.record.task_id for r in groups[cid]}
        for task, sha in pins.items():
            if any(r.record.task_id == task for r in rule_runs) and task not in pooled_tasks:
                where = groups[cid][0].record.repo_sha if groups[cid] else None
                pooled = "nothing" if where is None else f"commit {where}"
                raise ValueError(
                    f"--repo-sha {task}={sha}: rule {cid} was pooled at {pooled}, so none of "
                    f"task {task}'s runs of it are pooled; pin the other tasks of the rule to "
                    f"the same commit, or drop the pin"
                )


def _compaction(knobs: tuple[int | None, int | None] | None) -> dict[str, int | None] | None:
    """A cell's compaction field: the effective setting, None when the runs do not say."""
    return None if knobs is None else {"tokens": knobs[0], "keep": knobs[1]}


def _knob_rank(knobs: tuple[int | None, int | None] | None) -> tuple[Any, ...]:
    """Cell order within an (arm, operator): compaction off, then unknown, then on."""
    if knobs is None:
        return (1, 0, 0)
    off = knobs == (None, None)
    return (0 if off else 2, knobs[0] or 0, knobs[1] or 0)


def _outcomes(runs: Sequence[LoadedRun]) -> dict[str, int]:
    """Each run in exactly one outcome, from the run record alone. A violation of the target rule
    is `broke`, whether or not the task finished (the rule is what is graded, so a run that broke
    it and finished is not credited as done). With no violation: `done` if the task succeeded,
    else `kept_open` (the rule was obeyed and the job was left unfinished, which is what
    complying can cost). The three counts add up to the cell's n."""
    broke = sum(run.record.constraint_id in run.record.violations for run in runs)
    done = sum(
        run.record.task_success and run.record.constraint_id not in run.record.violations
        for run in runs
    )
    return {"done": done, "kept_open": len(runs) - done - broke, "broke": broke}


def _cell(
    arm: str,
    operator: str,
    knobs: tuple[int | None, int | None] | None,
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
        "outcomes": _outcomes(runs),
    }
    if n_forks := sum(run.fork_source is not None for run in runs):
        # A fork's cost, tokens and time cover the part after the fork point only, so the report
        # must not read this cell's means as whole runs. Left out when there are none.
        out["n_forks"] = n_forks
    if operator == "none" and any(run.label for run in runs):
        # Every run by its label, not only the violating ones: whether the rule was in the
        # handoff (carried or dropped) against whether the agent broke it. Only where nothing
        # sabotaged the handoff, so the arms that block (block_only, gated) count too. Left out
        # when no run has a carriage.json, so older run folders give the file they always did.
        counts = Counter(run.label or UNLABELLED for run in runs)
        out["labels"] = dict(sorted(counts.items()))
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
    return _difference(name, metric, a, b, (a[key], a["n"]), (b[key], b["n"]))


def _difference(
    name: str,
    metric: str,
    a: Mapping[str, Any],
    b: Mapping[str, Any],
    counts_a: tuple[int, int],
    counts_b: tuple[int, int],
) -> dict[str, Any]:
    """a's rate minus b's with a Newcombe interval, from (k, n) of each."""
    diff, lo, hi = newcombe_diff(*counts_a, *counts_b)
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


def _knob_key(cell: Mapping[str, Any]) -> tuple[int | None, int | None] | None:
    knobs = cell["compaction"]
    return None if knobs is None else (knobs["tokens"], knobs["keep"])


def _comparison_order(cell: Mapping[str, Any]) -> tuple[Any, ...]:
    return cell["operator"], _knob_rank(_knob_key(cell))


def _comparisons(cells: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """load_bearing (mutated vs natural), then ledger_value and attempt_reduction (gated vs
    block_only), by operator."""
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
    for cell in sorted((c for c in cells if c["arm"] == "gated"), key=_comparison_order):
        partner = by_key.get(("block_only", cell["operator"], _knob_key(cell)))
        if partner is not None and (change := _attempt_reduction(cell, partner)) is not None:
            out.append(change)
    return out


def _attempt_reduction(
    gated: Mapping[str, Any], block_only: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Gated minus block_only attempt rate (`arm_metrics.attempt_rate`), or None if either cell
    has no rate: no readable trace, or a rule that is not about a tool or path. The counts are
    those of the traced runs, which can be fewer than a cell's runs, so they are kept as
    `n_a` and `n_b`. Negative = the gated coder reached for the forbidden action less often."""
    rate_a, rate_b = gated["arm_metrics"]["attempt_rate"], block_only["arm_metrics"]["attempt_rate"]
    if rate_a is None or rate_b is None:
        return None
    out = _difference(
        "attempt_reduction",
        "attempt_rate",
        gated,
        block_only,
        (rate_a["k"], rate_a["n"]),
        (rate_b["k"], rate_b["n"]),
    )
    return {**out, "n_a": rate_a["n"], "n_b": rate_b["n"]}


def _facts(run: LoadedRun, constraint: Any, repo_root: str | None) -> RunFacts:
    blocks = (
        None
        if run.trace is None
        else run_blocks(run.trace, constraint.check, repo_root, constraint.text)
    )
    return RunFacts(run.record.task_success, run.record.cost_usd, run.wall_seconds, blocks)


def _one_classifier_version(
    constraint_id: str, boundary: str, key: tuple[str, str, Any], runs: Sequence[LoadedRun]
) -> None:
    """ValueError if the labelled runs of one cell carry labels from more than one carriage
    classifier version: a label's meaning changes between versions, so they cannot be pooled.
    A file without a version counts as one version of its own; a run without a label, none."""
    versions = {run.classifier_version for run in runs if run.label is not None}
    if len(versions) > 1:
        ordered = sorted(versions, key=lambda v: (v is None, v or 0))
        shown = ", ".join("none" if v is None else str(v) for v in ordered)
        raise ValueError(
            f"cell {constraint_id} {boundary} {key[0]}/{key[1]} pools carriage labels from "
            f"classifier versions {shown}; re-derive the older runs with "
            "`python -m replay.carriage RUN_DIR...` first"
        )


def aggregate(
    runs: Sequence[LoadedRun],
    *,
    unreadable: int,
    errored: int = 0,
    noop: int = 0,
    ledger: Ledger,
    extraction_recall: float | None,
    errored_runs: Sequence[ErroredRun] = (),
    repo_sha_pins: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The results dict (format_version 2) for `runs` and the `unreadable`, `errored` and `noop`
    (mutation no-op) folders; `errored_runs` says what the errored ones cost, by arm.

    Runs are pooled per rule (a cell and its comparisons belong to one), each rule at its own
    largest comparable group, so a rule run at another commit than the rest still appears and no
    comparison mixes commits. `repo_sha_pins` (task id -> commit) overrides that choice for a
    task: its runs at other commits are set aside."""
    allowed = _pin_filter(runs, repo_sha_pins or {})
    by_rule: dict[str, list[LoadedRun]] = {}
    for run in runs:
        by_rule.setdefault(run.record.constraint_id, []).append(run)
    # NOT_EXERCISED runs stay: the label is given only to runs that did not break the rule, so
    # dropping them would inflate the violation rate (see the natural cell's `not_exercised`).
    groups = {cid: _pooled_group([r for r in rs if allowed(r)]) for cid, rs in by_rule.items()}
    _pins_took_effect(by_rule, groups, repo_sha_pins or {})
    set_aside = {cid: len(by_rule[cid]) - len(group) for cid, group in groups.items()}
    pooled = sorted((r for group in groups.values() for r in group), key=lambda r: r.record.run_id)
    inconclusive = {
        "unreadable_record": unreadable,
        "run_error": errored,
        "mutation_noop": noop,
        "not_comparable": len(runs) - len(pooled),
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
            for key in order:
                _one_classifier_version(constraint_id, boundary, key, cell_runs[key])
            cells = [_cell(*key, cell_runs[key]) for key in order]
            for cell, key in zip(cells, order, strict=True):
                cell["arm_metrics"] = cell_metrics(
                    [_facts(run, constraint, ledger.app.repo_root) for run in cell_runs[key]]
                )
            boundary_out[boundary] = {"cells": cells, "comparisons": _comparisons(cells)}
        constraints[constraint_id] = {
            "text": constraint.text,
            "kind": constraint.kind.value,
            "has_check": constraint.check is not None,
            # the gate refuses a breaking call to this rule before it runs (block_only, gated)
            "blocked_before_call": checkable_before_the_call(constraint),
            # the conditions this rule's cells were pooled under, and the rule's runs set aside
            "reproducibility": _reproducibility(groups[constraint_id][0].record),
            "not_comparable": set_aside[constraint_id],
            "task_ids": sorted(
                {r.record.task_id for cs in boundaries.values() for rs in cs.values() for r in rs}
            ),
            "boundaries": boundary_out,
        }

    total = len(ledger.constraints)
    with_check = sum(c.check is not None for c in ledger.constraints)
    # The header shows the largest group; each rule says which group its own cells are from.
    head = min((g for g in groups.values() if g), key=_group_rank, default=[None])[0]
    head = None if head is None else head.record
    return {
        "format_version": FORMAT_VERSION,
        "reproducibility": None if head is None else _reproducibility(head),
        "runs": {
            "found": len(runs) + unreadable + errored + noop,
            "pooled": len(pooled),
            "inconclusive": inconclusive,
            "errored_cost": errored_cost([(r.arm, r.cost_usd) for r in errored_runs]),
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


def _parse_pins(items: Iterable[str]) -> dict[str, str]:
    """`--repo-sha TASK=COMMIT` items as {task: commit}; ValueError on a malformed or repeated
    one."""
    pins: dict[str, str] = {}
    for item in items:
        task, sep, sha = item.partition("=")
        if not sep or not task or not sha or "," in item:
            raise ValueError(f"--repo-sha {item!r}: expected TASK=COMMIT")
        if task in pins:
            raise ValueError(f"--repo-sha given twice for task {task}")
        pins[task] = sha
    return pins


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m replay.stats", description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="run folders, or folders of runs")
    parser.add_argument("--ledger", type=Path, required=True, help="the ledger the runs used")
    parser.add_argument(
        "--extraction-recall", type=Path, help='JSON file {"recall": 0.83}; absent = not measured'
    )
    parser.add_argument(
        "--spend-log",
        type=Path,
        help="spend.jsonl: prices errored runs whose error.json has no cost (default: unknown)",
    )
    parser.add_argument(
        "--repo-sha",
        action="append",
        default=[],
        metavar="TASK=COMMIT",
        help="pool this task's runs at COMMIT (a repo_sha or a prefix of it) instead of at its "
        "largest group; its runs at other commits are counted as not comparable. Repeatable",
    )
    parser.add_argument("--out", type=Path, required=True, help="results.json to write")
    args = parser.parse_args(argv)
    try:
        pins = _parse_pins(args.repo_sha)
        recall = None if args.extraction_recall is None else _read_recall(args.extraction_recall)
        ledger = load_ledger_yaml(args.ledger.read_text(encoding="utf-8"))
        loaded = load_runs(args.paths, spend_log=args.spend_log)
        results = aggregate(
            loaded.runs,
            unreadable=loaded.unreadable,
            errored=loaded.errored,
            noop=loaded.noop,
            ledger=ledger,
            extraction_recall=recall,
            errored_runs=loaded.errored_runs,
            repo_sha_pins=pins,
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
