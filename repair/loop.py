"""One repair iteration and its deterministic accept/reject (SPEC C5).

Accepted if and only if the patched runs break the target rule strictly less often than the
baseline and succeed at the task at least as often. No model decides the verdict.

A round that does not finish has no verdict, and says so. Its summary is written before the
re-runs start (`on_patch`), and a stopped round rewrites it with how far it got (`stopped_result`).
Both use the heading `# Repair: Rejected`, because that is all the results page reads, and start
their result with "Not judged": nothing is ever accepted without every re-run having finished.
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repair.evidence import Evidence
from repair.guards import GuardViolation, check_append_only, check_edits
from repair.menu import get_fix
from repair.patch import Edit, Patch, apply_edits, unified_diff, write_patch
from repair.plan import CallBudget, FixChoice, choose_fix
from schemas.run import RunRecord

# Fewest runs on each side before a verdict: one clean patched run must not beat a noisy
# baseline.
MIN_RUNS = 3
# Super writes the patch at most this often: once, and once more told why the guards refused it.
# Ultra's 3-call limit is separate (repair/plan.py).
MAX_WRITE_TRIES = 2


@dataclass(frozen=True)
class Rates:
    runs: int
    violated: int
    succeeded: int

    @property
    def cvr(self) -> float:
        return self.violated / self.runs if self.runs else 0.0

    @property
    def success(self) -> float:
        return self.succeeded / self.runs if self.runs else 0.0


@dataclass(frozen=True)
class RepairResult:
    accepted: bool
    reason: str
    choice: FixChoice | None
    edits: list[Edit] = field(default_factory=list)
    diff: str = ""
    before: Rates | None = None
    after: Rates | None = None


# Written before the re-runs start. If the process is killed outright it is the only summary
# there will be, so it must be true for that case and for a round that is still running.
PENDING_REASON = (
    "Not judged: the re-runs had started when this summary was written and it was never "
    "updated, so the process stopped before it reached a verdict, or is still running. "
    "Nothing is accepted. The diff is the patch that was being tested."
)


def pending_result(choice: FixChoice, edits: list[Edit], diff: str, before: Rates) -> RepairResult:
    """The round as it stands once the patch has passed the guards, before its re-runs start."""
    return RepairResult(False, PENDING_REASON, choice, edits, diff, before, None)


def _share(k: int, n: int) -> str:
    return f"{k} of {n} ({k / n:.0%})"


def stopped_result(
    pending: RepairResult,
    completed: Sequence[RunRecord],
    planned: int,
    why: str,
    constraint_id: str,
) -> RepairResult:
    """A round whose re-runs did not all finish: never accepted, and not judged either way.

    The partial numbers are stated as plain facts about the runs that finished. They are worded
    so that the results page, which turns "violation rate X% -> Y%" into a "Judged on" table,
    does not read them as a verdict.
    """
    after = rates(completed, constraint_id)
    reason = (
        f"Not judged: the round stopped ({why}) after {after.runs} of {planned} re-runs, so the "
        "accept rule was not applied and nothing is accepted."
    )
    before = pending.before
    if after.runs and before is not None and before.runs:
        reason += (
            f" On the {after.runs} runs that finished, {_share(after.violated, after.runs)} "
            f"broke the rule, against {_share(before.violated, before.runs)} of the baseline "
            f"runs, and {_share(after.succeeded, after.runs)} passed the task, against "
            f"{_share(before.succeeded, before.runs)}."
        )
    return RepairResult(
        False, reason, pending.choice, pending.edits, pending.diff, before,
        after if after.runs else None,
    )  # fmt: skip


def rates(records: Sequence[RunRecord], constraint_id: str) -> Rates:
    mine = [r for r in records if r.constraint_id == constraint_id]
    return Rates(
        runs=len(mine),
        violated=sum(constraint_id in r.violations for r in mine),
        succeeded=sum(r.task_success for r in mine),
    )


def _guarded(edits: Sequence[Edit], files: Mapping[str, str], ledger_file: str | None) -> dict:
    """The files after `edits`, or GuardViolation if the patch breaks a guard."""
    check_edits(edits, files)
    after_files = apply_edits(files, edits)
    if after_files == dict(files):
        raise GuardViolation("the patch makes no change")
    if ledger_file is not None and ledger_file in files:
        check_append_only(files[ledger_file], after_files[ledger_file])
    return after_files


def repair_once(
    *,
    evidence: Evidence,
    baseline: Sequence[RunRecord],
    files: Mapping[str, str],
    agents: Sequence[str],
    planner_model: Any,
    writer_model: Any,
    rerun: Callable[[Patch], list[RunRecord]],
    ledger_file: str | None = None,
    budget: CallBudget | None = None,
    on_patch: Callable[[RepairResult], None] | None = None,
) -> RepairResult:
    before = rates(baseline, evidence.constraint_id)
    choice = choose_fix(planner_model, evidence, agents, budget or CallBudget())
    fix = get_fix(choice.fix_id)
    rejected: tuple[list[Edit], str] | None = None
    for _ in range(MAX_WRITE_TRIES):
        edits = write_patch(writer_model, fix, choice, evidence, files, rejected=rejected)
        try:
            after_files = _guarded(edits, files, ledger_file)
            break
        except GuardViolation as exc:
            rejected = (edits, str(exc))
    else:
        reason = f"guard: {rejected[1]} ({MAX_WRITE_TRIES} tries)"
        return RepairResult(False, reason, choice, edits, "", before, None)
    diff = unified_diff(files, after_files)
    # The patch passed the guards: record it now, before the long part that can be killed.
    if on_patch is not None:
        on_patch(pending_result(choice, edits, diff, before))
    after = rates(rerun(Patch(choice.fix_id, edits, after_files)), evidence.constraint_id)
    reason = (
        f"violation rate {before.cvr:.0%} -> {after.cvr:.0%}, "
        f"task success {before.success:.0%} -> {after.success:.0%} "
        f"({before.runs} baseline runs, {after.runs} patched runs)"
    )
    if min(before.runs, after.runs) < MIN_RUNS:
        return RepairResult(
            False, f"too few runs to judge (need {MIN_RUNS} each): {reason}", choice, edits,
            diff, before, after,
        )  # fmt: skip
    accepted = after.cvr < before.cvr and after.success >= before.success
    return RepairResult(accepted, reason, choice, edits, diff, before, after)


def _write_atomically(path: Path, text: str) -> None:
    """Write then rename, so a kill during the write leaves the old file or the new one, never half
    of it: the results page refuses a summary with a heading and no result line."""
    scratch = path.with_name(f".{path.name}.tmp")
    scratch.write_text(text, encoding="utf-8")
    os.replace(scratch, path)


def write_report(
    result: RepairResult, out_dir: str | Path, kept: str | Path | None = None
) -> tuple[Path, Path]:
    """The PR-ready diff (`.patch`) and a short markdown summary next to it.

    `kept` is a folder holding re-run records that were left in place (a round that did not
    finish); it is named after the `- Diff:` line, where the results page does not read it as part
    of the result.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = f"repair-{result.choice.fix_id if result.choice else 'none'}"
    patch_path, summary_path = out / f"{name}.patch", out / f"{name}.md"
    _write_atomically(patch_path, result.diff)
    verdict = "Accepted" if result.accepted else "Rejected"
    lines = [
        f"# Repair: {verdict}",
        "",
        f"- Fix: `{result.choice.fix_id}` on `{result.choice.agent}`" if result.choice else "",
        f"- Why: {result.choice.rationale}" if result.choice else "",
        f"- Result: {result.reason}",
        f"- Diff: `{patch_path.name}`",
        f"- Re-runs kept in: `{kept}`" if kept is not None else "",
    ]
    _write_atomically(summary_path, "\n".join(line for line in lines if line != "") + "\n")
    return patch_path, summary_path
