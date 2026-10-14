"""Guards a patch must pass before it is applied or re-run (SPEC C5 and its risk row).

"Patch removes rules or dumps full history" (and AGENTS.md rule 8, never weaken a check) is
closed off by: a protected-path denylist that no allowlist can override (ledger, config,
pipeline code, tests, tasks and check scripts), an allowlist of editable files, insert-only
edits checked in order (old text kept verbatim, once), a token ceiling on what a patch may add,
and an append-only ledger.
"""

from collections.abc import Iterable, Mapping, Sequence
from pathlib import PurePosixPath

from repair.patch import Edit

MAX_ADDED_TOKENS = 400
CHARS_PER_TOKEN = 4  # the same deliberately rough ratio family as common/llm.py's estimate

# Folders whose files a patch may never touch: the pipeline, tests, tasks, docs, CI.
_PROTECTED_DIRS = frozenset(
    {
        "schemas",
        "common",
        "ledger",
        "oracle",
        "gate",
        "replay",
        "repair",
        "report",
        "preflight",
        "tests",
        "tasks",
        "docs",
        ".github",
    }
)
# Ledgers (their regexes are predicates), configuration and lock files.
_PROTECTED_SUFFIXES = frozenset({".yaml", ".yml", ".toml", ".lock"})


class GuardViolation(ValueError):
    """The patch breaks a repair guard and is rejected without being run."""


def protected_reason(path: str) -> str | None:
    """Why `path` can never be patched, or None if it may be."""
    parts = PurePosixPath(path.replace("\\", "/"))
    name = parts.name
    if name.startswith(".env"):
        return "secrets"
    if parts.suffix in _PROTECTED_SUFFIXES:
        return "a ledger, configuration or lock file"
    if any(part in _PROTECTED_DIRS for part in parts.parts[:-1]):
        return "pipeline, test, task, docs or CI folder"
    if name == "conftest.py" or name.startswith(("test_", "check_")) or name.endswith("_test.py"):
        return "a test or check script"
    return None


def check_editable(paths: Iterable[str]) -> None:
    for path in paths:
        reason = protected_reason(path)
        if reason is not None:
            raise GuardViolation(f"{path} is protected ({reason}) and can never be patched")


def check_edits(edits: Sequence[Edit], files: Mapping[str, str]) -> None:
    """Each edit is checked against the text as left by the edits before it, which is exactly
    how apply_edits applies them, so overlapping edits cannot slip through."""
    current = dict(files)
    added = 0
    for edit in edits:
        if edit.file not in files:
            raise GuardViolation(f"{edit.file} is not one of the editable files")
        check_editable([edit.file])
        text = current[edit.file]
        if not edit.old_string or text.count(edit.old_string) != 1:
            raise GuardViolation(f"old_string must occur exactly once in {edit.file}")
        if edit.old_string not in edit.new_string:
            raise GuardViolation(
                f"edits must be insert-only: new_string must keep old_string verbatim ({edit.file})"
            )
        current[edit.file] = text.replace(edit.old_string, edit.new_string, 1)
        added += len(edit.new_string) - len(edit.old_string)
    if added > MAX_ADDED_TOKENS * CHARS_PER_TOKEN:
        raise GuardViolation(
            f"the patch adds about {added // CHARS_PER_TOKEN} tokens, over the token ceiling of "
            f"{MAX_ADDED_TOKENS}"
        )


def check_append_only(before: str, after: str) -> None:
    if not after.startswith(before):
        raise GuardViolation("the ledger is append-only to the repair agent")
