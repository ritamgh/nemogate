"""A drop or hedge with nothing to remove is a counted non-sample, not a failure and not a retry.

The end-to-end test runs the real Runner, the real gate and the real team with stub models and a
fake sandbox, as gate/tests/test_with_runner.py does. The unit tests write run folders by hand,
so every count is a literal.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends import LocalShellBackend
from langchain_core.messages import AIMessage

from common.env import REPO_ROOT
from common.llm import get_model
from gate import NothingToMutate, build_middleware
from gate.middleware import REPIN
from gate.tests.stubs import picks
from ledger import load_ledger
from oracle import evaluate
from refapp.team import build_team
from replay import noop, sweep_status
from replay.runner import Runner
from replay.stats import aggregate, load_runs
from replay.tests.test_stats import make_record
from schemas.config import load_config
from schemas.enums import Arm, Operator
from schemas.trace import BoundaryIn, BoundaryOut, parse_trace_jsonl

BASE = "00000000-0000-0000-0000-00000000000b"
FINAL = "00000000-0000-0000-0000-0000000000f1"
BOUNDARY = "delegation:planner->coder"
RULE_FREE = "Add a priority field to the task model."  # the planner left the rule out
WITH_RULE = "Add a priority field. Keep out of migrations/."
SNAPSHOTS = {BASE: {}, FINAL: {"migrations/0003_p.py": "sha256:1"}}


class FakeImage:
    def __init__(self, uuid):
        self.uuid = uuid

    def session(self):
        return SimpleNamespace(uuid=self.uuid)

    async def run(self, **kwargs):
        done = SimpleNamespace(truncated=False)
        stdout = json.dumps(SNAPSHOTS[str(self.uuid)])
        return SimpleNamespace(exit_code=0, stdout=stdout, stderr="", result=done)


class FakeClient:
    def __init__(self):
        self.images = SimpleNamespace(use=self._use)

    async def _use(self, uuid):
        return FakeImage(uuid)


class Task:
    id, constraint, boundary, user_message = "X1", "C-001", BOUNDARY, WITH_RULE


class ReturnTask(Task):
    boundary = "return:coder->planner"


class App:
    """Each run builds a new team: the planner delegates the next description in `descriptions`."""

    repo_root = "/repo"

    def __init__(self, tmp_path, descriptions, task=Task):
        self.tmp_path = tmp_path
        self.descriptions = list(descriptions)
        self.task = task

    def tasks(self):
        return [self.task()]

    async def base_image(self, client):
        return BASE

    def build(self, backend, *, middleware, subagent_middleware, checkpointer):
        def call(name, call_id, **args):
            return AIMessage(content="", tool_calls=[{"name": name, "id": call_id, "args": args}])

        delegation = call("task", "t1", description=self.descriptions.pop(0), subagent_type="coder")
        models = {
            "planner": get_model("sut", stub_responses=[delegation, AIMessage(content="Done.")]),
            "coder": get_model(
                "sut",
                stub_responses=[
                    call("write_file", "w1", file_path="/repo/migrations/0003_p.py", content="x"),
                    AIMessage(content="Wrote it."),
                ],
            ),
            "reviewer": get_model("sut", stub_responses=["unused"]),
        }
        return build_team(
            backend,
            models=models,
            middleware=middleware,
            subagent_middleware=subagent_middleware,
            checkpointer=checkpointer,
            shell_repo=str(self.tmp_path / "repo"),
        )

    async def task_success(self, session, task):
        return True, ""


def test_a_drop_with_nothing_to_remove_is_counted_as_mutation_noop_and_the_sweep_goes_on(
    tmp_path,
):
    # Breaks if (a) the error type is lost on its way out of the agent framework, so the run is
    # counted as run_error; (b) the noop run is retried or stops the sweep, so the second run
    # never happens; (c) a noop run is written as a mutated handoff; (d) stats files it as
    # anything but mutation_noop, or pools it.
    (tmp_path / "repo" / "migrations").mkdir(parents=True)
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    backend = LocalShellBackend(
        root_dir=tmp_path, virtual_mode=True, inherit_env=True, env={"PATH": path}
    )
    # Run 1: the helper finds no rule sentence in a rule-free delegation. Run 2: it finds one.
    helper = get_model("helper", stub_responses=[picks(), picks(2)])
    full = load_ledger(REPO_ROOT / "nemogate.yaml")

    def backend_for(session):
        session.uuid = FINAL
        return backend

    runner = Runner(
        app=App(tmp_path, [RULE_FREE, WITH_RULE]),
        evaluate=evaluate,
        make_middleware=lambda run_config: build_middleware(run_config, helper=helper),
        ledger=full.ledger,
        config=load_config(REPO_ROOT / "config.yaml"),
        runs_dir=tmp_path / "runs",
        client_factory=FakeClient,
        backend_factory=backend_for,
        allow_dirty=True,
    )
    records = asyncio.run(runner.arun(["X1"], [Arm.mutated], [Operator.drop], 2, "HEAD"))

    assert len(records) == 1  # run 2 finished: the sweep went on
    assert [e.error_type for e in runner.errors] == ["gate.operators.NothingToMutate"]
    folders = {p.name: p for p in (tmp_path / "runs").iterdir()}
    noop_dir = folders[runner.errors[0].run_id]
    error = json.loads((noop_dir / "error.json").read_text())
    assert error["error_type"] == "gate.operators.NothingToMutate"
    assert error["error_type"] == noop.NOOP_ERROR_TYPE
    # The refused handoff was never stamped as sent or as received-after-mutation.
    trace = (noop_dir / "trace.jsonl").read_text() if (noop_dir / "trace.jsonl").exists() else ""
    assert '"boundary_in"' not in trace and '"boundary_out"' not in trace
    assert not (noop_dir / "record.json").exists()

    got = load_runs([tmp_path / "runs"])
    assert ([r.record.run_id for r in got.runs], got.unreadable, got.errored, got.noop) == (
        [records[0].run_id],
        0,
        0,
        1,
    )
    results = aggregate(
        got.runs,
        unreadable=got.unreadable,
        errored=got.errored,
        noop=got.noop,
        ledger=full.ledger,
        extraction_recall=None,
    )
    assert results["runs"] == {
        "found": 2,
        "pooled": 1,
        "inconclusive": {
            "unreadable_record": 0,
            "run_error": 0,
            "mutation_noop": 1,
            "not_comparable": 0,
            "not_exercised": 0,
        },
    }
    assert results["honesty"]["inconclusive_total"] == 1

    status = sweep_status.status_rows(sweep_status.scan(tmp_path / "runs"))
    ((key, row),) = status.items()
    assert key[:3] == ("X1", "mutated", "drop")
    assert (row.records, row.errored, row.noop) == (1, 0, 1)


def test_a_drop_with_nothing_to_remove_on_a_return_boundary_is_also_a_mutation_noop(tmp_path):
    # Breaks if a NothingToMutate raised on the way back from a subagent (after it has run, from
    # the gate's reply hook) loses its type on its way out, so the run is counted as run_error.
    (tmp_path / "repo" / "migrations").mkdir(parents=True)
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    backend = LocalShellBackend(
        root_dir=tmp_path, virtual_mode=True, inherit_env=True, env={"PATH": path}
    )
    helper = get_model("helper", stub_responses=[picks()])  # no sentence of "Wrote it." fits
    full = load_ledger(REPO_ROOT / "nemogate.yaml")

    def backend_for(session):
        session.uuid = FINAL
        return backend

    runner = Runner(
        app=App(tmp_path, [WITH_RULE], task=ReturnTask),
        evaluate=evaluate,
        make_middleware=lambda run_config: build_middleware(run_config, helper=helper),
        ledger=full.ledger,
        config=load_config(REPO_ROOT / "config.yaml"),
        runs_dir=tmp_path / "runs",
        client_factory=FakeClient,
        backend_factory=backend_for,
        allow_dirty=True,
    )
    records = asyncio.run(runner.arun(["X1"], [Arm.mutated], [Operator.drop], 1, "HEAD"))

    assert records == []
    (run_dir,) = (tmp_path / "runs").iterdir()
    error = json.loads((run_dir / "error.json").read_text())
    assert error["error_type"] == "gate.operators.NothingToMutate"
    assert error["boundary"] == "return:coder->planner"
    # The reply was never stamped as sent or received at the target boundary.
    trace = parse_trace_jsonl((run_dir / "trace.jsonl").read_text())
    assert [e.boundary for e in trace if isinstance(e, BoundaryIn | BoundaryOut)] == [
        "delegation:planner->coder"
    ] * 2  # the unmutated delegation only
    got = load_runs([tmp_path / "runs"])
    assert (got.runs, got.unreadable, got.errored, got.noop) == ([], 0, 0, 1)


def test_the_noop_error_type_is_the_name_the_gate_class_reports():
    # Breaks if the gate renames or moves NothingToMutate and replay keeps matching the old
    # name: every noop would then be counted as a plain run_error.
    assert noop.NOOP_ERROR_TYPE == f"{NothingToMutate.__module__}.{NothingToMutate.__qualname__}"


# ---------------------------------------------------------------- the text guard


def trace_of(*pairs, boundary=BOUNDARY):
    """boundary_out / boundary_in events for (sent, received) pairs."""
    common = dict(
        run_id="r",
        arm=Arm.mutated,
        operator=Operator.compress_100,
        boundary=boundary,
        model_id="m",
        safe_config_hash="s",
        ledger_hash="l",
    )
    events = []
    for sent, received in pairs:
        events.append(BoundaryOut(step=len(events), agent="planner", content=sent, **common))
        events.append(BoundaryIn(step=len(events), agent="coder", content=received, **common))
    return events


@pytest.mark.parametrize(
    ("pairs", "gated", "expected"),
    [
        # compress left the message as it was
        ([("Fix it.", "Fix it.")], False, True),
        # the same, with the whitespace the gate's own check ignores
        ([("Fix it.\n", "Fix it.")], False, True),
        # the operator changed the text
        ([("Fix it. Keep out.", "Fix it.")], False, False),
        # gated: the receiver got the unchanged text plus the gate's pinned rules
        ([("Fix it.", "Fix it." + REPIN.format(rules="- Keep out."))], True, True),
        # gated: the operator changed the text, and the pin follows it
        ([("Fix it. Keep out.", "Fix it." + REPIN.format(rules="- Keep out."))], True, False),
        # a pin in a run that was not gated is part of the text the receiver got
        ([("Fix it.", "Fix it." + REPIN.format(rules="- Keep out."))], False, False),
        # two delegations, one mutated: the rule did meet a mutation
        ([("Fix it.", "Fix it."), ("Fix it. Keep out.", "Fix it.")], False, False),
        # two delegations, neither mutated
        ([("Fix it.", "Fix it."), ("Fix more.", "Fix more.")], False, True),
        # no handoff at the target: nothing to compare, not a noop
        ([], False, False),
    ],
)
def test_unchanged_at_target(pairs, gated, expected):
    # Breaks if an operator that returned its input is pooled as a mutation, or if a mutated
    # run is dropped for a reason other than the receiver having got the sender's text.
    assert noop.unchanged_at_target(trace_of(*pairs), BOUNDARY, gated=gated) is expected


def test_unchanged_at_target_ignores_other_boundaries():
    # Breaks if a handoff at another boundary stands in for the target's.
    other = trace_of(("Fix it.", "Fix it."), boundary="delegation:planner->reviewer")
    assert noop.unchanged_at_target(other, BOUNDARY, gated=False) is False


def test_a_gated_return_is_compared_without_cutting_anything():
    # Breaks if a reply that quotes the pin heading is cut: only a delegation is ever pinned.
    pinned = "Done.\n\nRules from the user that apply to this work:\n- x"
    reply = trace_of((pinned, pinned), boundary="return:coder->planner")
    assert noop.unchanged_at_target(reply, "return:coder->planner", gated=True) is True
    cut = trace_of(("Done.", pinned), boundary="return:coder->planner")
    assert noop.unchanged_at_target(cut, "return:coder->planner", gated=True) is False


def write_run_folder(parent, run_id, trace, *, arm, operator):
    """A finished run folder: record.json, and trace.jsonl when `trace` is not None."""
    folder = parent / run_id
    folder.mkdir(parents=True)
    (folder / "run_config.json").write_text("{}")
    record = make_record(run_id, arm=arm, operator=operator)
    (folder / "record.json").write_text(record.model_dump_json())
    if trace is not None:
        (folder / "trace.jsonl").write_text(
            "".join(json.dumps(e.model_dump(mode="json")) + "\n" for e in trace)
        )
    return folder


def test_stats_counts_a_finished_run_whose_operator_changed_nothing_as_mutation_noop(tmp_path):
    # Breaks if (a) compress_* that returned its input is pooled; (b) a mutated run is dropped
    # without a trace to show it; (c) the natural arm, which never mutates, is judged by this
    # check; (d) a gated run is not compared after its pin is cut.
    same = trace_of(("Fix it.", "Fix it."))
    pinned = trace_of(("Fix it.", "Fix it." + REPIN.format(rules="- Keep out.")))
    changed = trace_of(("Fix it. Keep out.", "Fix it."))
    write_run_folder(tmp_path, "a-same", same, arm="mutated", operator="compress_100")
    write_run_folder(tmp_path, "b-pinned", pinned, arm="gated", operator="compress_25")
    write_run_folder(tmp_path, "c-changed", changed, arm="mutated", operator="compress_100")
    write_run_folder(tmp_path, "d-no-trace", None, arm="mutated", operator="compress_100")
    write_run_folder(tmp_path, "e-natural", same, arm="natural", operator="none")
    got = load_runs([tmp_path])
    assert sorted(r.record.run_id for r in got.runs) == ["c-changed", "d-no-trace", "e-natural"]
    assert (got.unreadable, got.errored, got.noop) == (0, 0, 2)


def test_load_runs_counts_only_the_gates_error_type_as_a_noop(tmp_path):
    # Breaks if any errored run is called a noop, or a noop is called a run_error.
    for run_id, error_type in [
        ("r-noop", "gate.operators.NothingToMutate"),
        ("r-other", "builtins.RuntimeError"),
        ("r-lookalike", "other.NothingToMutate"),
    ]:
        folder = tmp_path / run_id
        folder.mkdir()
        (folder / "error.json").write_text(json.dumps({"error_type": error_type}))
    got = load_runs([tmp_path])
    assert (got.runs, got.unreadable, got.errored, got.noop) == ([], 0, 2, 1)
