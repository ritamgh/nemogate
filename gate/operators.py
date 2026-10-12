"""Mutation operators applied to a handoff at the run's target boundary (SPEC, Operators)."""

from collections.abc import Callable
from typing import Any

from gate.text import message_text
from schemas.enums import Operator

DROP_PROMPT = """\
You edit a message that one agent hands to another. Remove every sentence or clause that \
states, restates or implies the rule below, in any wording. Change nothing else: keep every \
other instruction word for word, in the same order. If the message does not contain the rule, \
return it unchanged. Reply with the edited message only, with no commentary.

Rule:
{rule}

Message:
{message}"""


def make_operator(op: Operator, rule_text: str, model: Any) -> Callable[[str], str] | None:
    """The mutation for `op`, or None for `none`. `model` is the helper (get_model)."""
    if op is Operator.none:
        return None
    if op is not Operator.drop:
        raise NotImplementedError(f"operator {op.value} is built in week 2")

    def drop(message: str) -> str:
        prompt = DROP_PROMPT.format(rule=rule_text, message=message)
        reply = message_text(model.invoke(prompt).content)
        if not reply.strip():
            raise RuntimeError("drop: the helper returned an empty message")
        return reply.strip()

    return drop
