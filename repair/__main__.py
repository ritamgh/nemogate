"""`python -m repair`: one repair iteration on a lost rule, from existing run folders.

    NEMOGATE_LIVE=1 uv run python -m repair --runs runs --constraint <id> --task <id> \
        --editable <file> [<file> ...] --agents a,b,c --n N

Spends credit (Ultra and Super calls, then N re-runs per arm on the patched commit), so it
refuses without NEMOGATE_LIVE=1. Run `python -m replay.budget --bucket repair` first.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from common.env import REPO_ROOT, is_live
from repair.evidence import load_evidence, load_records
from repair.loop import repair_once, write_report
from repair.worktree import WorktreeRerun
from schemas.enums import Role


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m repair", description=__doc__)
    parser.add_argument("--runs", required=True, type=Path, help="folder of run folders")
    parser.add_argument("--constraint", required=True, help="the lost rule's id")
    parser.add_argument("--task", required=True, help="task id to re-run")
    parser.add_argument("--editable", required=True, nargs="+", help="files the patch may edit")
    parser.add_argument("--agents", required=True, help="comma-separated agent names")
    parser.add_argument("--arm", default="mutated")
    parser.add_argument("--operator", default="drop")
    parser.add_argument("--n", required=True, type=int, help="re-runs on the patched commit")
    parser.add_argument("--out", type=Path, default=Path("runs") / "repair")
    args = parser.parse_args(argv)

    if not is_live():
        print("refusing: repair calls Ultra and Super and re-runs tasks; set NEMOGATE_LIVE=1",
              file=sys.stderr)  # fmt: skip
        return 2
    from common.llm import get_model

    evidence = load_evidence(args.runs, args.constraint, args.task)
    found = load_records(
        args.runs, args.constraint, args.task, arm=args.arm, operator=args.operator
    )
    baseline = [record for _, record in found]
    if not baseline:
        print(f"no baseline runs for {args.task}/{args.arm}/{args.operator}", file=sys.stderr)
        return 2
    # Repo-relative paths, so the patch lands in the worktree, never in this checkout.
    files = {
        Path(path).resolve().relative_to(REPO_ROOT).as_posix(): Path(path).read_text("utf-8")
        for path in args.editable
    }
    result = repair_once(
        evidence=evidence,
        baseline=baseline,
        files=files,
        agents=[a.strip() for a in args.agents.split(",") if a.strip()],
        planner_model=get_model(Role.planner),
        writer_model=get_model(Role.sut),
        rerun=WorktreeRerun(
            repo_dir=REPO_ROOT,
            worktrees_dir=REPO_ROOT / "runs" / "repair-worktrees",
            tasks=[args.task],
            arms=[args.arm],
            operators=[args.operator],
            n=args.n,
        ),
    )
    patch_path, summary_path = write_report(result, args.out)
    print(f"{'ACCEPTED' if result.accepted else 'REJECTED'}: {result.reason}")
    print(f"diff: {patch_path}\nsummary: {summary_path}")
    return 0 if result.accepted else 1


if __name__ == "__main__":
    sys.exit(main())
