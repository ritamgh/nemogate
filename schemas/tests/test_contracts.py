import inspect

import pytest

from schemas.contracts import EvaluateFn, MakeMiddlewareFn, RunFn


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
    ("protocol", "names", "defaults"),
    [
        (EvaluateFn, ["ledger", "trace", "fs_diff"], {}),
        (RunFn, ["task_ids", "arms", "operators", "n", "repo_ref"], {"repo_ref": "main"}),
        (MakeMiddlewareFn, ["run_config"], {}),
    ],
    ids=["evaluate", "run", "make_middleware"],
)
def test_protocol_call_signature_is_pinned(protocol, names, defaults):
    # Breaks if a parameter is renamed, reordered, added, removed or given a different default:
    # isinstance() on a Protocol only checks that __call__ exists, so drift would go unnoticed.
    parameters = [
        p for p in inspect.signature(protocol.__call__).parameters.values() if p.name != "self"
    ]
    assert [p.name for p in parameters] == names
    assert {p.name: p.default for p in parameters if p.default is not p.empty} == defaults
