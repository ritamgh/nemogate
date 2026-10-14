"""One repair iteration and its deterministic accept/reject (SPEC C5).

Accepted if and only if the patched runs break the target rule strictly less often than the
baseline and succeed at the task at least as often. No model decides the verdict.
"""

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


def rates(records: Sequence[RunRecord], constraint_id: str) -> Rates:
    mine = [r for r in records if r.constraint_id == constraint_id]
    return Rates(
        runs=len(mine),
        violated=sum(constraint_id in r.violations for r in mine),
        succeeded=sum(r.task_success for r in mine),
    )


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
) -> RepairResult:
    before = rates(baseline, evidence.constraint_id)
    choice = choose_fix(planner_model, evidence, agents, budget or CallBudget())
    edits = write_patch(writer_model, get_fix(choice.fix_id), choice, evidence, files)
    try:
        check_edits(edits, files)
        after_files = apply_edits(files, edits)
        if after_files == dict(files):
            raise GuardViolation("the patch makes no change")
        if ledger_file is not None and ledger_file in files:
            check_append_only(files[ledger_file], after_files[ledger_file])
    except GuardViolation as exc:
        return RepairResult(False, f"guard: {exc}", choice, edits, "", before, None)
    diff = unified_diff(files, after_files)
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


def write_report(result: RepairResult, out_dir: str | Path) -> tuple[Path, Path]:
    """The PR-ready diff (`.patch`) and a short markdown summary next to it."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = f"repair-{result.choice.fix_id if result.choice else 'none'}"
    patch_path, summary_path = out / f"{name}.patch", out / f"{name}.md"
    patch_path.write_text(result.diff, encoding="utf-8")
    verdict = "Accepted" if result.accepted else "Rejected"
    lines = [
        f"# Repair: {verdict}",
        "",
        f"- Fix: `{result.choice.fix_id}` on `{result.choice.agent}`" if result.choice else "",
        f"- Why: {result.choice.rationale}" if result.choice else "",
        f"- Result: {result.reason}",
        f"- Diff: `{patch_path.name}`",
    ]
    summary_path.write_text("\n".join(line for line in lines if line != "") + "\n", "utf-8")
    return patch_path, summary_path
