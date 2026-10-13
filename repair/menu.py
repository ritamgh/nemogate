"""The closed fix menu (SPEC C5 guard): the only kinds of change the repair agent may make.

Each fix changes an agent's instructions; none removes anything (patches are insert-only, see
repair/guards.py), and none names a task, a rule or an app.
"""

from dataclasses import dataclass
from typing import Literal

FixId = Literal[
    "carry_rules",
    "require_output_format",
    "no_rule_compression",
    "check_reply_against_rules",
]


@dataclass(frozen=True)
class Fix:
    id: str
    applies_to: str  # "delegator", "delegate" or "any"
    instruction: str


MENU: tuple[Fix, ...] = (
    Fix(
        "carry_rules",
        "delegator",
        "Add an instruction telling the delegating agent to copy every rule the user stated, "
        "word for word, into each task it hands to another agent.",
    ),
    Fix(
        "require_output_format",
        "delegate",
        "Add an instruction telling the agent to reproduce exactly any output format or required "
        "line the user asked for in its reply, without paraphrasing it.",
    ),
    Fix(
        "no_rule_compression",
        "any",
        "Add an instruction never to summarise, shorten or drop a rule from the user when handing "
        "work on or reporting back; rules are always copied in full.",
    ),
    Fix(
        "check_reply_against_rules",
        "delegator",
        "Add an instruction telling the delegating agent to check every reply against the user's "
        "rules and, if one is missed, to delegate again and restate that rule.",
    ),
)


def get_fix(fix_id: str) -> Fix:
    for fix in MENU:
        if fix.id == fix_id:
            return fix
    raise KeyError(fix_id)
