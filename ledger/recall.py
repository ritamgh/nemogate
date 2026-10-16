"""Extraction recall on a seeded suite (SPEC honesty metric).

A declared constraint counts as recalled when extract+compile on the text that states it yields
exactly its check. The comparison is deterministic: no model judges whether a rule was found.

    NEMOGATE_LIVE=1 uv run python -m ledger.recall --cases CASES.yaml [--reps N] [--out FILE]

CASES.yaml holds `agents`, `tools` and `cases` (each a `name`, a `text` and the one `expect`ed
check, or null when the text states no rule). It prints, per case, whether the expected check
came back and the checks proposed beyond it ("extra"), then the totals.
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter

from common.env import is_live
from ledger.extract import propose
from schemas.enums import Role
from schemas.ledger import Check

_CHECK = TypeAdapter(Check)


@dataclass(frozen=True)
class RecallReport:
    matched: int
    total: int
    misses: list[str]

    @property
    def rate(self) -> float:
        return self.matched / self.total if self.total else 0.0


def recall(
    cases: Sequence[tuple[str, Check]], extract_checks: Callable[[str], list[Check | None]]
) -> RecallReport:
    """`cases` pairs each text with the declared check it states."""
    misses = [text for text, expected in cases if expected not in extract_checks(text)]
    return RecallReport(matched=len(cases) - len(misses), total=len(cases), misses=misses)


@dataclass(frozen=True)
class Case:
    name: str
    text: str
    expect: Check | None


def load_cases(path: Path) -> tuple[list[str], list[str], list[Case]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [
        Case(
            c["name"],
            c["text"],
            None if c["expect"] is None else _CHECK.validate_python(c["expect"]),
        )
        for c in data["cases"]
    ]
    return list(data["agents"]), list(data["tools"]), cases


def _dump(check: Check | None) -> Any:
    return None if check is None else check.model_dump(mode="json")


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {text}")
    return value


def main(argv: Sequence[str] | None = None, *, model: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ledger.recall", description=__doc__)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--reps", type=_positive, default=1, help="ask the helper this many times")
    parser.add_argument("--out", type=Path, help="write every proposal and the totals as JSON")
    args = parser.parse_args(argv)
    if model is None:
        if not is_live():
            print("refusing: recall calls the helper model; set NEMOGATE_LIVE=1", file=sys.stderr)
            return 2
        from common.llm import get_model

        model = get_model(Role.helper)
    agents, tools, cases = load_cases(args.cases)
    stated = [c for c in cases if c.expect is not None]
    rows, matched, extra = [], 0, 0
    for rep in range(args.reps):
        for case in cases:
            proposals = propose(model, case.text, agents, tools)
            checks = [p.check for p in proposals]
            hit = case.expect is not None and case.expect in checks
            # Which extracted rule compiled to the hit, so a reader can see it was the right one
            # (compile sees the whole text, so a stray rule could compile to the expected check).
            by = next((p.rule.text for p in proposals if hit and p.check == case.expect), None)
            more = [c for c in checks if c is not None and c != case.expect]
            matched += hit
            extra += len(more)
            mark = "-" if case.expect is None else ("ok  " if hit else "MISS")
            print(f"{mark} {case.name}: {[_dump(c) for c in checks]}")
            if by is not None:
                print(f"     from rule: {by!r}")
            rows.append({"rep": rep, "name": case.name, "expect": _dump(case.expect), "hit": hit,
                         "matched_rule": by, "rules": [p.rule.text for p in proposals],
                         "checks": [_dump(c) for c in checks], "extra": len(more)})  # fmt: skip
    total = len(stated) * args.reps
    print(f"recall {matched}/{total}; extra checks {extra} over {len(cases) * args.reps} texts")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        summary = {"matched": matched, "total": total, "extra": extra, "rows": rows}
        args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
