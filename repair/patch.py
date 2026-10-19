"""Super writes the patch as insert-only edits; the code applies them after the guards pass."""

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from repair.evidence import Evidence
from repair.menu import Fix

WRITE_PROMPT = """\
You change the instructions of a team of AI agents to apply one fix.

Fix: {fix}
Apply it to the instructions of the agent named {agent}, in the files below.
The rule from the user that must survive handoffs: {rule}

Only insert text. Each edit names a file, an exact passage that occurs exactly once in that file \
(old_string), and the same passage with your addition included (new_string). Make old_string a \
whole line of the instruction text, copied character for character, and put your addition after \
it. Do not anchor on the line that opens or closes a quoted string (the one with the name, the \
equals sign and the quote marks): its quotes and backslashes are easy to mistype. Keep additions \
short and general: do not paste conversation history or task details.

{files}"""

RETRY_PROMPT = """

A guard rejected your previous edits, so nothing was applied: {error}
Your previous edits:
{edits}
Write the edits again so that they pass. Copy each old_string exactly from its file, and \
include enough of the text around it (a whole line or more) that it occurs exactly once.{hints}"""


HINT_HEADER = (
    "\n\nWhole lines of {file} that occur exactly once (anchor on one of these, copied exactly):\n"
)
MAX_HINTS = 3  # per rejected edit
MIN_HINT_CHARS = 12
_QUOTE_DELIMITERS = ('"""', "'''")


class Edit(BaseModel):
    file: str
    old_string: str
    new_string: str


class EditList(BaseModel):
    edits: list[Edit]


@dataclass(frozen=True)
class Patch:
    fix_id: str
    edits: list[Edit]
    files_after: dict[str, str] = field(default_factory=dict)


def _files_text(files: Mapping[str, str]) -> str:
    return "\n\n".join(f"=== {path} ===\n{text}" for path, text in files.items())


def anchor_hints(edits: Sequence[Edit], files: Mapping[str, str]) -> str:
    """For each edit whose old_string does not occur exactly once in its file (the guard's rule),
    the whole lines of that file closest to it that do occur exactly once, copied verbatim.
    Lines that open or close a quoted string are left out: they are the ones the writer mistypes.
    """
    blocks = []
    for edit in edits:
        text = files.get(edit.file)
        if text is None or (edit.old_string and text.count(edit.old_string) == 1):
            continue
        unique = [
            line
            for line in dict.fromkeys(text.splitlines())
            if len(line.strip()) >= MIN_HINT_CHARS
            and not any(mark in line for mark in _QUOTE_DELIMITERS)
            and text.count(line) == 1
        ]
        close: list[str] = []
        for wanted in filter(str.strip, edit.old_string.splitlines()):
            close += difflib.get_close_matches(wanted, unique, n=2, cutoff=0.5)
        chosen = list(dict.fromkeys(close))[:MAX_HINTS] or unique[:MAX_HINTS]
        if chosen:
            blocks.append(HINT_HEADER.format(file=edit.file) + "\n".join(chosen))
    return "".join(blocks)


def write_patch(
    model: Any,
    fix: Fix,
    choice: Any,
    evidence: Evidence,
    files: Mapping[str, str],
    *,
    rejected: tuple[Sequence[Edit], str] | None = None,
) -> list[Edit]:
    """Super's edits for `fix`. `rejected` is (the previous edits, the guard's reason): the
    retry shows the writer what failed, and the guards check the new edits all over again."""
    prompt = WRITE_PROMPT.format(
        fix=fix.instruction, agent=choice.agent, rule=evidence.rule, files=_files_text(files)
    )
    if rejected is not None:
        edits, error = rejected
        listed = EditList(edits=list(edits)).model_dump_json(indent=2)
        hints = anchor_hints(edits, files)
        prompt += RETRY_PROMPT.format(error=error, edits=listed, hints=hints)
    return list(model.with_structured_output(EditList).invoke(prompt).edits)


def apply_edits(files: Mapping[str, str], edits: Sequence[Edit]) -> dict[str, str]:
    """`files` with each edit applied once (call the guards first)."""
    after = dict(files)
    for edit in edits:
        after[edit.file] = after[edit.file].replace(edit.old_string, edit.new_string, 1)
    return after


def unified_diff(before: Mapping[str, str], after: Mapping[str, str]) -> str:
    chunks = []
    for path in sorted(after):
        if before.get(path) == after[path]:
            continue
        chunks.extend(
            difflib.unified_diff(
                before.get(path, "").splitlines(keepends=True),
                after[path].splitlines(keepends=True),
                f"a/{path}",
                f"b/{path}",
            )
        )
    return "".join(chunks)
