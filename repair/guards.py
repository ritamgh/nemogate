"""Guards a patch must pass before it is applied or re-run (SPEC C5 and its risk row).

"Patch removes rules or dumps full history" is closed off by: an allowlist of editable files,
insert-only edits (old text kept verbatim), a token ceiling on what a patch may add, and an
append-only ledger.
"""

from collections.abc import Mapping, Sequence

from repair.patch import Edit

MAX_ADDED_TOKENS = 400
CHARS_PER_TOKEN = 4  # the same deliberately rough ratio family as common/llm.py's estimate


class GuardViolation(ValueError):
    """The patch breaks a repair guard and is rejected without being run."""


def check_edits(edits: Sequence[Edit], files: Mapping[str, str]) -> None:
    added = 0
    for edit in edits:
        if edit.file not in files:
            raise GuardViolation(f"{edit.file} is not one of the editable files")
        if not edit.old_string or files[edit.file].count(edit.old_string) != 1:
            raise GuardViolation(f"old_string must occur exactly once in {edit.file}")
        if edit.old_string not in edit.new_string:
            raise GuardViolation(
                f"edits must be insert-only: new_string must keep old_string verbatim ({edit.file})"
            )
        added += len(edit.new_string) - len(edit.old_string)
    if added > MAX_ADDED_TOKENS * CHARS_PER_TOKEN:
        raise GuardViolation(
            f"the patch adds about {added // CHARS_PER_TOKEN} tokens, over the token ceiling of "
            f"{MAX_ADDED_TOKENS}"
        )


def check_append_only(before: str, after: str) -> None:
    if not after.startswith(before):
        raise GuardViolation("the ledger is append-only to the repair agent")
