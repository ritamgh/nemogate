"""Extraction recall on a seeded suite (SPEC honesty metric).

A declared constraint counts as recalled when extract+compile on the text that states it yields
exactly its check. The comparison is deterministic: no model judges whether a rule was found.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from schemas.ledger import Check


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
