"""Mutation operators applied to a handoff at the run's target boundary (SPEC, Operators).

drop and hedge never let the helper edit text. The code splits the message into numbered
sentences, the helper names the numbers of the sentences that carry the rule (structured
output), and the code removes those sentences (drop) or swaps in the helper's vague version
(hedge). Live probes on the helper (FACTS §6, 2026-10-15): a free-form rewrite came back
unchanged, and free quoting also picked unrelated sentences; numbered picking was exact. On a
delegation the rule travels as an instruction; on a return what carries it is the content that
meets it, so each has its own prompt. compress ignores the rule.

The unit is the whole sentence: in "Do the work but never touch X." drop removes the work
instruction too, and hedge rewrites all of it. A drop or hedge that leaves the message as it was
raises NothingToMutate, so a run whose handoff was not mutated is never recorded as mutated.
"""

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from gate.text import message_text
from schemas.enums import Operator

SELECT_PROMPT = """\
Below are a rule and a message one agent hands to another, split into numbered sentences. Give \
the numbers of the sentences that state, restate or imply the rule, in any wording. Most \
sentences are about other things: leave those out. If no sentence concerns the rule, give an \
empty list.

Rule: {rule}

Sentences:
{sentences}"""

SELECT_RETURN_PROMPT = """\
Below are a rule and a reply one agent sends back to the agent that delegated work to it, split \
into numbered sentences. Give the numbers of the sentences that meet, follow or report on the \
rule. Most sentences are about other things: leave those out. If no sentence concerns the rule, \
give an empty list.

Rule: {rule}

Sentences:
{sentences}"""

HEDGE_PROMPT = """\
Below are a rule and a message one agent hands to another, split into numbered sentences. For \
each sentence that states the rule, give its number and a replacement sentence that turns it \
into vague, optional advice (for example "use your judgement about ..." or "consider ..."), so \
that it is no longer an instruction. Most sentences are about other things: leave those out. If \
no sentence concerns the rule, give an empty list.

Rule: {rule}

Sentences:
{sentences}"""

HEDGE_RETURN_PROMPT = """\
Below are a rule and a reply one agent sends back to the agent that delegated work to it, split \
into numbered sentences. For each sentence that meets or reports on the rule, give its number \
and a replacement sentence that is vague and noncommittal, keeping none of its exact wording or \
format. Most sentences are about other things: leave those out. If no sentence concerns the \
rule, give an empty list.

Rule: {rule}

Sentences:
{sentences}"""

COMPRESS_PROMPT = """\
Summarise the message below in at most {words} words. Keep what the recipient needs in order \
to act on it. Reply with the summary only, with no commentary.

Message:
{message}"""


class Picks(BaseModel):
    numbers: list[int]


class Rewrite(BaseModel):
    number: int
    replacement: str


class Rewrites(BaseModel):
    rewrites: list[Rewrite]


_SELECT = {"delegation": SELECT_PROMPT, "return": SELECT_RETURN_PROMPT}
_HEDGE = {"delegation": HEDGE_PROMPT, "return": HEDGE_RETURN_PROMPT}
_COMPRESS_WORDS = {Operator.compress_100: 100, Operator.compress_25: 25}
_WORD = re.compile(r"\S+")
# After . ! ? and a space (not after e.g. / i.e.), after a CJK full stop, or at line breaks.
_BREAK = re.compile(r"(?<=[.!?])(?<!e\.g\.)(?<!i\.e\.)[ \t]+|(?<=[。！？])[ \t]*|\n+")
_MARKER = re.compile(r"[ \t]*\d{1,3}[.)]")  # "1." or "2)" on its own: opens a list item


class NothingToMutate(RuntimeError):
    """drop or hedge found nothing to change: the handoff no longer carries the rule (for
    example the planner already left it out), so the run is not a sample of the mutation."""


def sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence: split after . ! ? and at line breaks. A list marker at the
    start of a line or after a sentence stays with the item that follows it. In "Do: 1. A. 2. B."
    the first marker stays with "Do:", since it cannot be told from a number ending a sentence."""
    found, pos = [], 0
    for match in _BREAK.finditer(text):
        piece = text[pos : match.start()]
        if _MARKER.fullmatch(piece) and "\n" not in match.group():
            continue  # keep pos: the marker opens the next sentence
        if piece.strip():
            found.append((pos, match.start()))
        pos = match.end()
    if text[pos:].strip():
        found.append((pos, len(text)))
    return found


def numbered(text: str) -> str:
    return "\n".join(f"{i}. {text[s:e]}" for i, (s, e) in enumerate(sentences(text), start=1))


def first_words(text: str, n: int) -> str:
    """The first `n` words of `text`, keeping its original spacing and line breaks."""
    matches = list(_WORD.finditer(text))
    if len(matches) <= n:
        return text
    return text[: matches[n - 1].end()] if n > 0 else ""


def _cut(text: str, start: int, end: int) -> str:
    """`text` without [start, end), rejoined cleanly."""
    before, after = text[:start], text[end:]
    if (not before or before.endswith("\n")) and (not after or after.startswith("\n")):
        # A whole line: drop it with one of its line breaks.
        return before + after[1:] if after else before[:-1]
    left, right = before.rstrip(" \t"), after.lstrip(" \t")
    gap = "" if not left or left.endswith("\n") or not right or right.startswith("\n") else " "
    return left + gap + right


def _chosen(text: str, numbers: list[int]) -> list[tuple[int, int, int]]:
    """(number, start, end) of each sentence named by a valid 1-based number, last first so
    edits to later sentences leave earlier positions intact."""
    spans = sentences(text)
    picked = {n for n in numbers if 1 <= n <= len(spans)}
    return [(n, *spans[n - 1]) for n in sorted(picked, reverse=True)]


def _ask(op: Operator, model: Any, schema: type[BaseModel], prompt: str) -> Any:
    """The helper's structured answer; a reply that does not parse raises rather than mutating
    nothing and being recorded as a mutation."""
    answer = model.with_structured_output(schema).invoke(prompt)
    if answer is None:
        raise RuntimeError(f"{op.value}: the helper's reply did not parse as {schema.__name__}")
    return answer


def _checked(op: Operator, text: str) -> str:
    text = text.strip()
    if not text:
        raise RuntimeError(f"{op.value}: the mutation left an empty message")
    return text


def _mutated(op: Operator, message: str, out: str) -> str:
    out = _checked(op, out)
    if out == message.strip():
        raise NothingToMutate(f"{op.value}: no sentence of the message carries the rule")
    return out


def make_operator(
    op: Operator, rule_text: str, model: Any, *, boundary: str = "delegation"
) -> Callable[[str], str] | None:
    """The mutation for `op` on a `delegation` or `return` boundary, or None for `none`.

    `model` is the helper (get_model). A mutation that would leave an empty message raises, so
    a failed mutation is never recorded as a successful one; one that changes nothing raises
    NothingToMutate.
    """
    if boundary not in ("delegation", "return"):
        raise ValueError(f"operators apply to a delegation or return boundary, not {boundary!r}")
    if op is Operator.none:
        return None

    if op is Operator.drop:

        def drop(message: str) -> str:
            prompt = _SELECT[boundary].format(rule=rule_text, sentences=numbered(message))
            numbers = _ask(op, model, Picks, prompt).numbers
            out = message
            for _, start, end in _chosen(message, numbers):
                out = _cut(out, start, end)
            return _mutated(op, message, out)

        return drop

    if op is Operator.hedge:

        def hedge(message: str) -> str:
            prompt = _HEDGE[boundary].format(rule=rule_text, sentences=numbered(message))
            items = _ask(op, model, Rewrites, prompt).rewrites
            replacement = {item.number: item.replacement for item in items}
            out = message
            for number, start, end in _chosen(message, list(replacement)):
                new = replacement[number].strip()
                out = out[:start] + new + out[end:] if new else _cut(out, start, end)
            return _mutated(op, message, out)

        return hedge

    if op in _COMPRESS_WORDS:
        words = _COMPRESS_WORDS[op]

        def compress(message: str) -> str:
            reply = model.invoke(COMPRESS_PROMPT.format(words=words, message=message))
            return _checked(op, first_words(message_text(reply.content).strip(), words))

        return compress
    raise NotImplementedError(f"operator {op.value} is not built (stretch)")
