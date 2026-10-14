"""Mutation operators applied to a handoff at the run's target boundary (SPEC, Operators).

On a delegation the rule travels as an instruction; on a return what carries it is the content
that meets it, so drop and hedge have one prompt per boundary kind. compress ignores the rule.
"""

import re
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

DROP_RETURN_PROMPT = """\
You edit a reply that one agent sends back to the agent that delegated work to it. Remove \
every part of the reply that meets, follows or reports on the rule below. Change nothing else: \
keep every other sentence word for word, in the same order. If no part of the reply concerns \
the rule, return it unchanged. Reply with the edited reply only, with no commentary.

Rule:
{rule}

Reply:
{message}"""

HEDGE_PROMPT = """\
You edit a message that one agent hands to another. Rewrite every sentence or clause that \
states the rule below so that it becomes vague, optional advice (for example "use your \
judgement about ..." or "consider ...") instead of an instruction. Change nothing else: keep \
every other instruction word for word, in the same order. If the message does not contain the \
rule, return it unchanged. Reply with the edited message only, with no commentary.

Rule:
{rule}

Message:
{message}"""

HEDGE_RETURN_PROMPT = """\
You edit a reply that one agent sends back to the agent that delegated work to it. Rewrite \
every part of the reply that meets or reports on the rule below so that it becomes vague and \
noncommittal, keeping none of its exact wording or format. Change nothing else: keep every \
other sentence word for word, in the same order. If no part of the reply concerns the rule, \
return it unchanged. Reply with the edited reply only, with no commentary.

Rule:
{rule}

Reply:
{message}"""

COMPRESS_PROMPT = """\
Summarise the message below in at most {words} words. Keep what the recipient needs in order \
to act on it. Reply with the summary only, with no commentary.

Message:
{message}"""

_RULE_PROMPTS = {
    (Operator.drop, "delegation"): DROP_PROMPT,
    (Operator.drop, "return"): DROP_RETURN_PROMPT,
    (Operator.hedge, "delegation"): HEDGE_PROMPT,
    (Operator.hedge, "return"): HEDGE_RETURN_PROMPT,
}
_COMPRESS_WORDS = {Operator.compress_100: 100, Operator.compress_25: 25}
_WORD = re.compile(r"\S+")


def first_words(text: str, n: int) -> str:
    """The first `n` words of `text`, keeping its original spacing and line breaks."""
    matches = list(_WORD.finditer(text))
    if len(matches) <= n:
        return text
    return text[: matches[n - 1].end()] if n > 0 else ""


def make_operator(
    op: Operator, rule_text: str, model: Any, *, boundary: str = "delegation"
) -> Callable[[str], str] | None:
    """The mutation for `op` on a `delegation` or `return` boundary, or None for `none`.

    `model` is the helper (get_model). An empty helper reply raises, so a failed mutation is
    never recorded as a successful one.
    """
    if boundary not in ("delegation", "return"):
        raise ValueError(f"operators apply to a delegation or return boundary, not {boundary!r}")
    if op is Operator.none:
        return None

    def ask(prompt: str) -> str:
        reply = message_text(model.invoke(prompt).content).strip()
        if not reply:
            raise RuntimeError(f"{op.value}: the helper returned an empty message")
        return reply

    if (op, boundary) in _RULE_PROMPTS:
        template = _RULE_PROMPTS[(op, boundary)]
        return lambda message: ask(template.format(rule=rule_text, message=message))
    if op in _COMPRESS_WORDS:
        words = _COMPRESS_WORDS[op]
        return lambda message: first_words(
            ask(COMPRESS_PROMPT.format(words=words, message=message)), words
        )
    raise NotImplementedError(f"operator {op.value} is not built (stretch)")
