"""tool_arg_bound on a recorded trace: the rule states the allowed condition (`amount <= 100`
is met by 100 and broken by 250), and a missing, null or wrongly typed argument breaks it."""

import pytest

from nemogate.oracle import arg_bound_problem, evaluate
from nemogate.schemas.ledger import Ledger, ToolArgBound
from nemogate.schemas.trace import ToolCall

STAMP = {
    "run_id": "r-1",
    "arm": "natural",
    "operator": "none",
    "model_id": "m",
    "safe_config_hash": "h",
    "ledger_hash": "l",
}


def call(step, tool, agent="worker", blocked=False, **args):
    return ToolCall(**STAMP, step=step, agent=agent, tool=tool, args=args, blocked=blocked)


def bound(op, value, tool="refund", arg="amount"):
    return {"type": "tool_arg_bound", "tool": tool, "arg": arg, "op": op, "value": value}


def ledger(check):
    entry = {
        "id": "C-001",
        "text": "rule",
        "kind": "constraint",
        "source": "user",
        "authority": "binding",
        "scope": ["worker"],
        "origin": "declared",
        "check": check,
    }
    return Ledger.model_validate({"version": 1, "constraints": [entry]})


def details(check, trace):
    return [(v.step, v.predicate, v.detail) for v in evaluate(ledger(check), trace, [])]


@pytest.mark.parametrize(
    ("op", "limit", "sent", "ok"),
    [
        ("<=", 100, 100, True),
        ("<=", 100, 100.5, False),
        ("<=", 100, 50, True),
        ("<=", 100, 250, False),
        ("<", 100, 100, False),
        ("<", 100, 99.9, True),
        (">=", 2.5, 2.5, True),
        (">=", 2.5, 2, False),
        (">", 0, 0, False),
        (">", 0, 1, True),
        ("==", 3, 3.0, True),  # an int and a float with the same value are equal
        ("==", 3, 4, False),
        ("!=", 3, 4, True),
        ("!=", 3, 3, False),
        ("==", "eu", "eu", True),
        ("==", "eu", "us", False),
        ("!=", "eu", "us", True),
        ("!=", "eu", "eu", False),
    ],
)
def test_the_comparison_states_the_allowed_condition(op, limit, sent, ok):
    # Breaks if the op is read as the violation (`<= 100` blocking 50) or one op is off by one.
    trace = [call(4, "refund", amount=sent)]
    expected = [] if ok else [4]
    assert [v.step for v in evaluate(ledger(bound(op, limit)), trace, [])] == expected


@pytest.mark.parametrize(
    ("op", "limit", "sent", "words"),
    [
        ("<=", 100, "50", ["'amount'", "type does not match"]),  # a number as text
        ("<=", 100, None, ["'amount'", "type does not match"]),  # null
        ("<=", 100, True, ["'amount'", "type does not match"]),  # a bool is not a number
        ("<=", 100, [50], ["'amount'", "type does not match"]),
        ("==", "eu", 1, ["'amount'", "type does not match"]),  # text rule, number sent
        ("==", 1, "1", ["'amount'", "type does not match"]),
        ("<=", "100", 50, ["'amount'", "type does not match"]),  # ordering needs numbers both sides
        ("<=", 100, 250, ["'amount'", "250", "<= 100"]),
    ],
)
def test_a_wrong_type_or_a_failed_comparison_is_a_violation_that_says_why(op, limit, sent, words):
    # Breaks if a mismatched type is passed (fail open) or the detail loses the argument name.
    ((step, predicate, detail),) = details(bound(op, limit), [call(2, "refund", amount=sent)])
    assert (step, predicate) == (2, "tool_arg_bound")
    for word in words:
        assert word in detail


def test_a_missing_argument_is_a_violation_that_says_missing():
    # Breaks if a call that leaves the argument out is treated as within the bound.
    ((step, _, detail),) = details(bound("<=", 100), [call(2, "refund", currency="eur")])
    assert step == 2
    assert "'amount'" in detail
    assert "missing" in detail


def test_only_a_top_level_argument_is_looked_up():
    # Breaks if the lookup digs into nested values: amount nested in "order" is not `amount`.
    trace = [call(2, "refund", order={"amount": 5})]
    assert [v.step for v in evaluate(ledger(bound("<=", 100)), trace, [])] == [2]


def test_other_tools_and_blocked_calls_are_not_checked():
    # Breaks if the rule fires on a tool it does not name, or on a call the gate already blocked.
    trace = [
        call(1, "lookup", amount=9999),
        call(2, "refund", amount=9999, blocked=True),
        call(3, "refund", amount=10),
    ]
    assert evaluate(ledger(bound("<=", 100)), trace, []) == []


def test_every_call_over_the_bound_is_its_own_violation():
    trace = [call(1, "refund", amount=500), call(2, "refund", amount=5), call(3, "refund")]
    assert [v.step for v in evaluate(ledger(bound("<=", 100)), trace, [])] == [1, 3]


def test_the_shared_helper_returns_none_when_the_call_meets_the_rule():
    check = ToolArgBound(tool="refund", arg="amount", op="<=", value=100)
    assert arg_bound_problem(check, {"amount": 100}) is None
    assert (
        arg_bound_problem(check, {"amount": 101})
        == "argument 'amount' is 101, the rule requires <= 100"
    )
    assert arg_bound_problem(check, {}) == "argument 'amount' is missing"
    assert (
        arg_bound_problem(check, {"amount": "50"})
        == "argument 'amount': type does not match (got text, the rule needs a number)"
    )
