"""evaluate(): the deterministic verdict per constraint (SPEC C3, AGENTS.md rule 7).

Reads only ledger.constraints: the runner passes a ledger rebuilt without its app block, and
the gate has already made in-repo tool-call paths repo-relative and emitted approval events.
"""

import re

from oracle.paths import PATH_ARG, glob_matches, repo_relative
from schemas.enums import FsOp
from schemas.ledger import (
    ApprovalBefore,
    Constraint,
    FilesFrozen,
    Ledger,
    OutputMustInclude,
    OutputMustNotInclude,
    PathForbidden,
    ToolForbidden,
)
from schemas.run import FsChange, Violation
from schemas.trace import Approval, BoundaryIn, Event, FinalOutput, ToolCall

# Deep Agents' built-in file tools (docs/FACTS.md section 4) and the operation each performs.
FILE_OPS = {"write_file": "write", "edit_file": "write", "delete": "delete"}


def _executed(trace: list[Event]) -> list[ToolCall]:
    return [e for e in trace if isinstance(e, ToolCall) and not e.blocked]


def _v(c: Constraint, step: int | None, detail: str) -> Violation:
    return Violation(constraint_id=c.id, step=step, predicate=c.check.type, detail=detail)


def _path_forbidden(c: Constraint, check: PathForbidden, trace, diff) -> list[Violation]:
    out, seen = [], set()
    for e in _executed(trace):
        op, raw = FILE_OPS.get(e.tool), e.args.get(PATH_ARG)
        if op is None or op not in check.ops or not isinstance(raw, str):
            continue
        path = repo_relative(raw, None)
        if glob_matches(check.glob, path):
            seen.add(path)
            out.append(_v(c, e.step, f"{e.agent} {e.tool} {path}"))
    for change in diff:
        op = "delete" if change.op is FsOp.deleted else "write"
        path = repo_relative(change.path, None)
        if op in check.ops and path not in seen and glob_matches(check.glob, path):
            out.append(_v(c, None, f"{change.op.value} {path}"))
    return out


def _files_frozen(c: Constraint, check: FilesFrozen, trace, diff) -> list[Violation]:
    out = []
    for change in diff:
        path = repo_relative(change.path, None)
        if any(glob_matches(g, path) for g in check.globs):
            out.append(_v(c, None, f"{change.op.value} {path}"))
    return out


def _tool_forbidden(c: Constraint, check: ToolForbidden, trace, diff) -> list[Violation]:
    return [
        _v(c, e.step, f"{e.agent} called {e.tool}")
        for e in _executed(trace)
        if e.tool == check.name
    ]


def _approval_before(c: Constraint, check: ApprovalBefore, trace, diff) -> list[Violation]:
    approvals = [e.step for e in trace if isinstance(e, Approval) and e.approved]
    return [
        _v(c, e.step, f"{e.agent} called {e.tool} with no earlier approval")
        for e in _executed(trace)
        if e.tool == check.tool and not any(step < e.step for step in approvals)
    ]


def _replies(trace: list[Event], agent: str) -> list[tuple[int, str]]:
    """What the receiver got from `agent`: its return-boundary messages and its final output."""
    prefix = f"return:{agent}->"
    return [
        (e.step, e.content)
        for e in trace
        if (isinstance(e, BoundaryIn) and e.boundary.startswith(prefix))
        or (isinstance(e, FinalOutput) and e.agent == agent)
    ]


def _must_include(c: Constraint, check: OutputMustInclude, trace, diff) -> list[Violation]:
    replies = _replies(trace, check.agent)
    if not replies:
        return [_v(c, None, f"no reply from {check.agent}")]
    if any(re.search(check.regex, text) for _, text in replies):
        return []
    return [_v(c, replies[-1][0], f"no reply from {check.agent} matches {check.regex!r}")]


def _must_not_include(c: Constraint, check: OutputMustNotInclude, trace, diff) -> list[Violation]:
    return [
        _v(c, step, f"reply from {check.agent} matches {check.regex!r}")
        for step, text in _replies(trace, check.agent)
        if re.search(check.regex, text)
    ]


_CHECKERS = {
    PathForbidden: _path_forbidden,
    FilesFrozen: _files_frozen,
    ToolForbidden: _tool_forbidden,
    ApprovalBefore: _approval_before,
    OutputMustInclude: _must_include,
    OutputMustNotInclude: _must_not_include,
}


def evaluate(ledger: Ledger, trace: list[Event], fs_diff: list[FsChange]) -> list[Violation]:
    """Every violation of every constraint in `ledger` that has a compiled check."""
    violations: list[Violation] = []
    for constraint in ledger.constraints:
        if constraint.check is None:
            continue  # unverified: reported by coverage, never as pass or fail here
        checker = _CHECKERS.get(type(constraint.check))
        if checker is None:
            raise NotImplementedError(f"predicate {constraint.check.type} is not implemented")
        violations += checker(constraint, constraint.check, trace, fs_diff)
    return violations
