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
(old_string), and the same passage with your addition included (new_string). Keep additions \
short and general: do not paste conversation history or task details.

{files}"""


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


def write_patch(
    model: Any, fix: Fix, choice: Any, evidence: Evidence, files: Mapping[str, str]
) -> list[Edit]:
    prompt = WRITE_PROMPT.format(
        fix=fix.instruction, agent=choice.agent, rule=evidence.rule, files=_files_text(files)
    )
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
