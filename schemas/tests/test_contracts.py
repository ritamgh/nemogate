import inspect
import typing
from typing import Any

import pytest

from schemas.contracts import EvaluateFn, MakeMiddlewareFn, RunFn
from schemas.enums import Arm, Operator
from schemas.ledger import Ledger
from schemas.run import FsChange, RunConfig, RunRecord, Violation
from schemas.trace import Event


def test_cross_owner_protocols_accept_functions_with_the_agreed_signatures():
    # Breaks if schemas.contracts stops importing, or a Protocol stops being runtime-checkable.
    def evaluate(ledger, trace, fs_diff):
        return []

    def run(task_ids, arms, operators, n, repo_ref="main"):
        return []

    def make_middleware(run_config):
        return []

    assert isinstance(evaluate, EvaluateFn)
    assert isinstance(run, RunFn)
    assert isinstance(make_middleware, MakeMiddlewareFn)
    assert not isinstance("not callable", EvaluateFn)


@pytest.mark.parametrize(
    ("protocol", "names", "defaults", "annotations", "returns"),
    [
        (
            EvaluateFn,
            ["ledger", "trace", "fs_diff"],
            {},
            {"ledger": Ledger, "trace": list[Event], "fs_diff": list[FsChange]},
            list[Violation],
        ),
        (
            RunFn,
            ["task_ids", "arms", "operators", "n", "repo_ref"],
            {"repo_ref": "main"},
            {
                "task_ids": list[str],
                "arms": list[Arm],
                "operators": list[Operator],
                "n": int,
                "repo_ref": str,
            },
            list[RunRecord],
        ),
        (
            MakeMiddlewareFn,
            ["run_config"],
            {},
            {"run_config": RunConfig},
            list[Any],
        ),
    ],
    ids=["evaluate", "run", "make_middleware"],
)
def test_protocol_call_signature_is_pinned(protocol, names, defaults, annotations, returns):
    # Breaks if a parameter is renamed, reordered, added, removed, given a different default,
    # made keyword-only, or re-annotated, or if the return type changes:
    # isinstance() on a Protocol only checks that __call__ exists, so drift would go unnoticed.
    parameters = [
        p for p in inspect.signature(protocol.__call__).parameters.values() if p.name != "self"
    ]
    assert [p.name for p in parameters] == names
    assert {p.name: p.default for p in parameters if p.default is not p.empty} == defaults
    assert {p.name: p.kind for p in parameters} == dict.fromkeys(
        names, inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    hints = typing.get_type_hints(protocol.__call__, include_extras=True)
    assert hints.pop("return") == returns
    assert hints == annotations
