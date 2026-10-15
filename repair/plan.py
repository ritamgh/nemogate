"""Ultra picks one fix from the closed menu (SPEC C5), at most 3 Ultra calls per iteration."""

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from repair.evidence import Evidence
from repair.menu import MENU, FixId, get_fix

MAX_ULTRA_CALLS = 3  # SPEC Budget rules: "Ultra: repair planning, at most 3 calls per iteration"

PLAN_PROMPT = """\
You repair a team of AI agents named: {agents}. A rule from the user is being lost when work is \
handed from one agent to another. Choose exactly one fix from the menu below and the agent whose \
instructions it should change, and give a one-line reason.

Rule: {rule}
Kind of check: {check_type}
Handoff where it is lost: {boundary}
Broken in {violated} of {runs} runs.
Example, as sent: {sent}
Example, as received: {received}

Menu (fix id, which agent it suits, what it does):
{menu}"""


class BudgetExceeded(RuntimeError):
    """More Ultra calls than SPEC allows in one repair iteration."""


class CallBudget:
    def __init__(self, limit: int = MAX_ULTRA_CALLS) -> None:
        self.limit = limit
        self.used = 0

    def spend(self) -> None:
        if self.used >= self.limit:
            raise BudgetExceeded(f"Ultra call budget of {self.limit} per iteration is used up")
        self.used += 1


class FixChoice(BaseModel):
    fix_id: FixId
    agent: str
    rationale: str


def _menu_text() -> str:
    return "\n".join(f"- {f.id} ({f.applies_to}): {f.instruction}" for f in MENU)


def choose_fix(
    model: Any, evidence: Evidence, agents: Sequence[str], budget: CallBudget
) -> FixChoice:
    budget.spend()
    prompt = PLAN_PROMPT.format(
        agents=", ".join(agents),
        rule=evidence.rule,
        check_type=evidence.check_type,
        boundary=evidence.boundary,
        violated=evidence.violated,
        runs=evidence.runs,
        sent=evidence.example_sent,
        received=evidence.example_received,
        menu=_menu_text(),
    )
    choice = model.with_structured_output(FixChoice).invoke(prompt)
    get_fix(choice.fix_id)  # the Literal already restricts it; the menu is the authority
    if choice.agent not in agents:
        raise ValueError(f"the planner chose agent {choice.agent!r}, which the team does not have")
    return choice
