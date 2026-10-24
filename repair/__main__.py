"""`python -m repair`: one repair iteration on a lost rule, from existing run folders.

    NEMOGATE_LIVE=1 uv run python -m repair --runs runs --constraint <id> --task <id> \
        --editable <file> [<file> ...] --agents a,b,c --n N [--yes]

Spends credit (Ultra and Super calls, then N re-runs on the patched commit), so it refuses
without NEMOGATE_LIVE=1. Without --yes it prints the planned re-runs and what the repair bucket
allows, and stops; it also refuses an --n larger than the bucket allows (AGENTS.md rule 3).
Editable files are read from HEAD, so uncommitted edits never reach the repair branch, and
protected files (ledger, config, pipeline, tests, tasks, check scripts) are refused outright.

Exit status: 0 accepted, 1 rejected, 2 refused before spending, 3 not judged. A round is not
judged when it is stopped (Ctrl-C, SIGTERM) or its re-runs fail after the patch passed the guards.
The summary and the diff are written as soon as the patch passes the guards, so even a kill that
cannot be caught leaves them behind; a stopped round rewrites the summary with how many re-runs
finished and keeps its worktree, whose `runs/` folder holds the records that did. A round writes
`repair-<fix>.md` in --out, so give each round its own --out to keep an earlier round's summary.
"""

import argparse
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from common.env import REPO_ROOT, is_live
from common.spend import total_spent
from repair.evidence import load_evidence, load_records
from repair.guards import GuardViolation, check_editable
from repair.loop import MIN_RUNS, RepairResult, repair_once, stopped_result, write_report
from repair.worktree import WorktreeRerun
from replay.budget import mean_cost, plan
from schemas.config import load_config
from schemas.enums import Arm, Operator, Role

NOT_JUDGED = 3  # exit status of a round that did not finish


def _stop_as_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt(signal.Signals(signum).name)


def _why(exc: BaseException) -> str:
    """Why the round stopped, in words from a fixed set: nothing a model or a run wrote."""
    if isinstance(exc, KeyboardInterrupt):
        name = str(exc.args[0]) if exc.args else ""
        return f"stopped by {name}" if name.startswith("SIG") else "interrupted"
    if isinstance(exc, subprocess.CalledProcessError):
        return f"the runner exited with status {exc.returncode}"
    return f"failed with {type(exc).__name__}"


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
    pending: list[RepairResult] = []  # the round as it stood when its re-runs started

    def on_patch(result: RepairResult) -> None:
        pending.append(result)
        write_report(result, args.out)

    def on_start(tree: Path, branch: str) -> None:
        write_report(pending[0], args.out, kept=tree)  # so a kill still says where the runs are

    if rerun is None:
        rerun = WorktreeRerun(
            repo_dir=REPO_ROOT,
            worktrees_dir=REPO_ROOT / "runs" / "repair-worktrees",
            tasks=[args.task],
            arms=[args.arm],
            operators=[args.operator],
            n=args.n,
            on_start=on_start,
        )
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, _stop_as_interrupt)
    try:
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
            on_patch=on_patch,
        )
    except (Exception, KeyboardInterrupt) as exc:
        if not pending:  # nothing was tested yet, so there is no round to report
            raise
        return _report_not_judged(exc, pending[0], rerun, args)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    if not result.accepted and result.after is not None:
        rerun.discard_last()  # a re-run happened: drop the rejected patch's worktree and branch
    patch_path, summary_path = write_report(result, args.out)
    print(f"{'ACCEPTED' if result.accepted else 'REJECTED'}: {result.reason}")
    print(f"diff: {patch_path}\nsummary: {summary_path}")
    kept = getattr(rerun, "last", None)
    if result.accepted and kept is not None:
        print(f"patched commit: local branch {kept[1]} (worktree {kept[0]})")
    return 0 if result.accepted else 1


def _report_not_judged(
    exc: BaseException, pending: RepairResult, rerun: Any, args: argparse.Namespace
) -> int:
    """Write the summary of a round that did not finish, and keep what it has done.

    The worktree is not discarded: the re-runs that finished are records in it, and they cost
    credit. The summary names it.
    """
    completed = getattr(rerun, "completed", None)
    finished = completed() if callable(completed) else []
    stopped = stopped_result(pending, finished, args.n, _why(exc), args.constraint)
    last = getattr(rerun, "last", None)
    kept = last[0] if last is not None else None
    patch_path, summary_path = write_report(stopped, args.out, kept=kept)
    print(f"NOT JUDGED: {stopped.reason}", file=sys.stderr)
    if not isinstance(exc, KeyboardInterrupt):
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"diff: {patch_path}\nsummary: {summary_path}")
    if kept is not None:
        print(f"re-run records kept in: {kept}")
    return NOT_JUDGED


if __name__ == "__main__":
    sys.exit(main())
