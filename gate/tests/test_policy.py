"""Pre-call blocking reuses the oracle, so the gate never blocks what the oracle would pass."""

import pytest

from gate.policy import blocking_constraint, checkable_before_the_call
from schemas.ledger import Ledger
from schemas.trace import Approval, ToolCall

STAMP = {
    "run_id": "r-1",
    "arm": "block_only",
    "operator": "drop",
    "model_id": "m",
    "safe_config_hash": "h",
    "ledger_hash": "l",
}


def call(step, tool, **args):
    return ToolCall(**STAMP, step=step, agent="worker", tool=tool, args=args)


def approval(step, approved=True):
    return Approval(**STAMP, step=step, agent="worker", action="remove x", approved=approved)


def ledger(check, scope=("worker",)):
    entry = {
        "id": "C-001",
        "text": "rule",
        "kind": "constraint",
        "source": "user",
        "authority": "binding",
        "scope": list(scope),
        "origin": "declared",
        "check": check,
    }
    return Ledger.model_validate({"version": 1, "constraints": [entry]})


PATH = {"type": "path_forbidden", "glob": "vendor/**", "ops": ["write", "delete"]}


@pytest.mark.parametrize(
    ("tool", "path", "blocked"),
    [
        ("write_file", "vendor/x.py", True),
        ("edit_file", "vendor/x.py", True),
        ("delete", "vendor", True),
        ("read_file", "vendor/x.py", False),
        ("write_file", "src/x.py", False),
    ],
)
def test_path_forbidden_blocks_writes_and_deletes_under_the_glob(tool, path, blocked):
    hit = blocking_constraint(ledger(PATH), [], call(3, tool, file_path=path))
    assert (hit is not None) is blocked


def test_tool_forbidden_blocks_the_named_tool_only():
    check = {"type": "tool_forbidden", "name": "fetch"}
    assert blocking_constraint(ledger(check), [], call(1, "fetch", url="u")).id == "C-001"
    assert blocking_constraint(ledger(check), [], call(1, "read_file", file_path="a")) is None


@pytest.mark.parametrize(
    ("prior", "blocked"),
    [([], True), ([approval(1)], False), ([approval(1, approved=False)], True)],
    ids=["no-approval", "approved-earlier", "denied-earlier"],
)
def test_approval_before_blocks_until_an_earlier_approval(prior, blocked):
    check = {"type": "approval_before", "tool": "remove"}
    hit = blocking_constraint(ledger(check), prior, call(5, "remove", file_path="a"))
    assert (hit is not None) is blocked


@pytest.mark.parametrize(
    "check",
    [
        {"type": "files_frozen", "globs": ["deps.txt"]},
        {"type": "output_must_include", "regex": "^NOTE:", "agent": "worker"},
        {"type": "output_must_not_include", "regex": "x", "agent": "worker"},
        None,
    ],
    ids=["files_frozen", "must_include", "must_not_include", "unverified"],
)
def test_checks_not_decidable_before_the_call_never_block(check):
    # SPEC predicate table: files_frozen is judged after the run and output checks have no
    # call to stop, so block-only must not invent a policy for them.
    hit = blocking_constraint(ledger(check), [], call(1, "write_file", file_path="deps.txt"))
    assert hit is None


def test_checkable_before_the_call_matches_the_spec_table():
    assert checkable_before_the_call(ledger(PATH).constraints[0])
    files_frozen = ledger({"type": "files_frozen", "globs": ["a"]}).constraints[0]
    assert not checkable_before_the_call(files_frozen)
