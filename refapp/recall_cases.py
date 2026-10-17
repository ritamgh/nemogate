"""Extraction-recall cases for the reference app, built from the task files and the ledger.

    uv run python -m refapp.recall_cases --out runs/recall/reference.yaml
    NEMOGATE_LIVE=1 uv run python -m ledger.recall --cases runs/recall/reference.yaml --reps 3

One case per task in refapp/tasks/: the task's user message, expecting the check its target
constraint has in nemogate.yaml. Built, not copied, so the cases can never drift from the tasks.
The sealed tasks are not in this repo and so never in these cases (AGENTS.md rule 6). These are
the reference tasks, not a held-out set: the extraction prompts were improved knowing the kinds
of misses they had (docs/FACTS.md §6).
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

from ledger import load_ledger
from refapp.tasks import load_tasks
from refapp.team import AGENTS
from refapp.tools import make_tools

ROOT = Path(__file__).resolve().parents[1]
# Deep Agents' built-in file tools as the agents get them, `execute` turned off (FACTS §4).
FILE_TOOLS = ("ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep")


def tools() -> list[str]:
    return [*FILE_TOOLS, *make_tools(None)]  # building the custom tools runs nothing


def cases(ledger_path: Path = ROOT / "nemogate.yaml") -> dict:
    ledger = load_ledger(ledger_path).ledger
    for task in load_tasks():
        if ledger.get(task.constraint).check is None:
            raise ValueError(f"{task.id}: {task.constraint} has no check, so recall cannot hit it")
    return {
        "agents": list(AGENTS),
        "tools": tools(),
        "cases": [
            {
                "name": f"{task.id} {task.constraint}",
                "text": task.user_message,
                "expect": ledger.get(task.constraint).check.model_dump(mode="json"),
            }
            for task in load_tasks()
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m refapp.recall_cases", description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(yaml.safe_dump(cases(), sort_keys=False, width=100), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
