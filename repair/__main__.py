"""`python -m repair`: one repair iteration on a lost rule, from existing run folders.

    NEMOGATE_LIVE=1 uv run python -m repair --runs runs --constraint <id> --task <id> \
        --editable <file> [<file> ...] --agents a,b,c --n N [--yes]

Spends credit (Ultra and Super calls, then N re-runs on the patched commit), so it refuses
without NEMOGATE_LIVE=1. Without --yes it prints the planned re-runs and what the repair bucket
allows, and stops; it also refuses an --n larger than the bucket allows (AGENTS.md rule 3).
Editable files are read from HEAD, so uncommitted edits never reach the repair branch, and
protected files (ledger, config, pipeline, tests, tasks, check scripts) are refused outright.
"""

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from common.env import REPO_ROOT, is_live
from common.spend import total_spent
from repair.evidence import load_evidence, load_records
from repair.guards import GuardViolation, check_editable
from repair.loop import MIN_RUNS, repair_once, write_report
from repair.worktree import WorktreeRerun
from replay.budget import mean_cost, plan
from schemas.config import load_config
from schemas.enums import Arm, Operator, Role


def _repo_relative(path: str) -> str:
    return Path(path).resolve().relative_to(REPO_ROOT).as_posix()


def _at_head(path: str) -> str:
    return subprocess.run(
        ["git", "show", f"HEAD:{path}"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def main(
    argv: Sequence[str] | None = None,
    *,
    planner_model: Any = None,
    writer_model: Any = None,
    rerun: Any = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m repair", description=__doc__)
    parser.add_argument("--runs", required=True, type=Path, help="folder of run folders")
    parser.add_argument("--constraint", required=True, help="the lost rule's id")
    parser.add_argument("--task", required=True, help="task id to re-run")
    parser.add_argument("--editable", required=True, nargs="+", help="files the patch may edit")
    parser.add_argument("--agents", required=True, help="comma-separated agent names")
    parser.add_argument("--arm", default="mutated", choices=[a.value for a in Arm])
    parser.add_argument("--operator", default="drop", choices=[o.value for o in Operator])
    parser.add_argument("--n", required=True, type=int, help="re-runs on the patched commit")
    parser.add_argument("--ledger", default="nemogate.yaml", help="the ledger (append-only)")
    parser.add_argument("--out", type=Path, default=Path("runs") / "repair")
    parser.add_argument("--yes", action="store_true", help="confirm the planned re-runs")
    args = parser.parse_args(argv)

    if not is_live():
        print("refusing: repair calls Ultra and Super and re-runs tasks; set NEMOGATE_LIVE=1",
              file=sys.stderr)  # fmt: skip
        return 2
    try:
        editable = [_repo_relative(path) for path in args.editable]
        ledger_file = _repo_relative(args.ledger)
        check_editable(editable)
    except GuardViolation as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    except ValueError:
        print(f"refusing: every path must be inside the repository ({REPO_ROOT}); run from "
              "its root", file=sys.stderr)  # fmt: skip
        return 2

    found = load_records(
        args.runs, args.constraint, args.task, arm=args.arm, operator=args.operator
    )
    baseline = [record for _, record in found]
    if not baseline:
        print(f"no baseline runs for {args.task}/{args.arm}/{args.operator}", file=sys.stderr)
        return 2
    cost = mean_cost(baseline)
    if not cost > 0:
        print("refusing: the baseline runs carry no cost, so the re-run cannot be sized",
              file=sys.stderr)  # fmt: skip
        return 2
    budget = plan(
        bucket="repair",
        cost_per_run=cost,
        arms=[Arm(args.arm)],
        operators=[Operator(args.operator)],
        n_tasks=1,
        config=load_config(REPO_ROOT / "config.yaml"),
        spent_usd=total_spent(),
    )
    print(
        f"planned re-runs: {args.n} (about ${args.n * cost:.2f} at ${cost:.4f} per run); "
        f"the repair bucket allows {budget.n}"
    )
    if args.n < MIN_RUNS:
        print(f"refusing: --n {args.n} re-runs can never be accepted; a verdict needs at least "
              f"{MIN_RUNS}", file=sys.stderr)  # fmt: skip
        return 2
    if args.n > budget.n:
        print(f"refusing: --n {args.n} is more than the repair bucket allows ({budget.n})",
              file=sys.stderr)  # fmt: skip
        return 2
    if not args.yes:
        print("not started: check the plan above, then repeat with --yes", file=sys.stderr)
        return 1

    if planner_model is None or writer_model is None:
        from common.llm import get_model

        planner_model = planner_model or get_model(Role.planner)
        writer_model = writer_model or get_model(Role.sut)
    if rerun is None:
        rerun = WorktreeRerun(
            repo_dir=REPO_ROOT,
            worktrees_dir=REPO_ROOT / "runs" / "repair-worktrees",
            tasks=[args.task],
            arms=[args.arm],
            operators=[args.operator],
            n=args.n,
        )
    result = repair_once(
        evidence=load_evidence(
            args.runs, args.constraint, args.task, arm=args.arm, operator=args.operator
        ),
        baseline=baseline,
        files={path: _at_head(path) for path in editable},
        agents=[a.strip() for a in args.agents.split(",") if a.strip()],
        planner_model=planner_model,
        writer_model=writer_model,
        rerun=rerun,
        ledger_file=ledger_file,
    )
    if not result.accepted and result.after is not None:
        rerun.discard_last()  # a re-run happened: drop the rejected patch's worktree and branch
    patch_path, summary_path = write_report(result, args.out)
    print(f"{'ACCEPTED' if result.accepted else 'REJECTED'}: {result.reason}")
    print(f"diff: {patch_path}\nsummary: {summary_path}")
    kept = getattr(rerun, "last", None)
    if result.accepted and kept is not None:
        print(f"patched commit: local branch {kept[1]} (worktree {kept[0]})")
    return 0 if result.accepted else 1


if __name__ == "__main__":
    sys.exit(main())
