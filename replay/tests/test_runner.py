"""replay/runner.py with fakes for Contree, the app and the gate/oracle: no sandbox, no model."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage

from common import ConfigError, MissingEnvError, SpendCapReached, env
from common.spend import append_spend
from replay import record, runner
from replay.runner import Runner
from schemas.config import load_config
from schemas.enums import Arm, FsOp, Operator, Role
from schemas.ledger import Constraint, Ledger, ToolForbidden, load_ledger_yaml
from schemas.run import FsChange, RunConfig, RunRecord, Violation
from schemas.trace import BoundaryIn, BoundaryOut, ToolCall, dump_event_jsonl

ROOT = Path(__file__).resolve().parents[2]
BASE = "00000000-0000-0000-0000-00000000000b"
FINAL = "00000000-0000-0000-0000-0000000000f1"
SNAPSHOTS = {
    BASE: {"a.py": "sha256:1", "b.py": "sha256:2"},
    FINAL: {"a.py": "sha256:9", "c.py": "sha256:3"},
}
EXPECTED_DIFF = [
    FsChange(path="a.py", op=FsOp.modified),
    FsChange(path="b.py", op=FsOp.deleted),
    FsChange(path="c.py", op=FsOp.added),
]


class FakeSession:
    def __init__(self, uuid: str):
        self.uuid = UUID(uuid)


class FakeImage:
    def __init__(self, client, uuid: UUID):
        self.client, self.uuid = client, uuid

    def session(self) -> FakeSession:
        session = FakeSession(str(self.uuid))
        self.client.sessions.append(session)
        return session

    async def run(self, **kwargs):
        return SimpleNamespace(
            exit_code=0,
            stdout=json.dumps(SNAPSHOTS[str(self.uuid)]),
            stderr="",
            result=SimpleNamespace(truncated=False),
        )


class FakeClient:
    def __init__(self):
        self.used: list[UUID] = []
        self.sessions: list[FakeSession] = []
        self.images = SimpleNamespace(use=self._use)

    async def _use(self, uuid: UUID) -> FakeImage:
        self.used.append(uuid)
        return FakeImage(self, uuid)


class FakeMiddleware:
    """What the fake gate hands back: it holds the RunConfig, so the graph finds the trace path."""

    def __init__(self, run_config: RunConfig):
        self.run_config = run_config


class FakeGraph:
    def __init__(self, session, run_config, checkpointer, behaviour, number=0):
        self.session, self.run_config, self.number = session, run_config, number
        self.checkpointer, self.behaviour = checkpointer, behaviour
        self.invoked: list[tuple] = []
        self.state_configs: list[dict] = []

    async def ainvoke(self, payload, config):
        self.invoked.append((payload, config))
        await self.checkpointer.setup()  # creates the real sqlite file
        if self.behaviour.get("raises"):
            raise ValueError(self.behaviour["raises"])
        if self.number in self.behaviour.get("raises_on", ()):  # the n-th run built (1-based)
            raise ValueError(f"boom in run {self.number}")
        if self.number in self.behaviour.get("stops_on", {}):
            raise self.behaviour["stops_on"][self.number]
        if "trace_text" in self.behaviour:
            Path(self.run_config.trace_path).write_text(self.behaviour["trace_text"])
        elif self.behaviour.get("write_trace", True):
            events = trace_events(
                self.run_config,
                ledger_hash=self.behaviour.get("trace_ledger_hash", LEDGER_HASH),
                bad=self.behaviour.get("bad_event"),
                handoff=self.behaviour.get("handoff"),
            )
            Path(self.run_config.trace_path).write_text(
                "\n".join(dump_event_jsonl(e) for e in events) + "\n", encoding="utf-8"
            )
        # run_totals only sees these if the runner put the call inside run_scope(run_id)
        append_spend(Role.sut, "m-sut", 100, 50, 0.01)
        append_spend(Role.planner, "m-planner", 10, 5, 0.02)
        self.session.uuid = UUID(FINAL)

    async def aget_state(self, config):
        self.state_configs.append(config)
        event = self.behaviour.get("summarization_event")
        values = {"_summarization_event": event} if event else {}
        return SimpleNamespace(config={"configurable": {"checkpoint_id": "ckpt-7"}}, values=values)

    async def aget_state_history(self, config):
        # Newest first: the checkpoint after the delegating model call holds the event and the
        # `task` call together; the oldest holds neither.
        event = self.behaviour.get("summarization_event")
        delegation = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "id": "c1",
                    "args": {"subagent_type": "coder"},
                    "type": "tool_call",
                }
            ],
        )
        yield SimpleNamespace(
            config={"configurable": {"checkpoint_id": "ckpt-7"}},
            parent_config={"configurable": {"checkpoint_id": "ckpt-0"}},
            metadata={"step": 3},
            values={"_summarization_event": event, "messages": [delegation]},
        )
        yield SimpleNamespace(
            config={"configurable": {"checkpoint_id": "ckpt-0"}},
            parent_config=None,
            metadata={"step": 0},
            values={"messages": []},
        )


def trace_events(rc: RunConfig, *, ledger_hash: str | None = None, bad=None, handoff=None) -> list:
    """Two events carrying the hashes a correct gate writes. `bad` = (step, field, value) swaps
    one field of one event, as a gate hashing another ledger or config would. `handoff` =
    (sent, received) adds the planner's boundary_out and boundary_in at the run's target."""
    fields = {
        "run_id": rc.run_id,
        "safe_config_hash": SAFE_CONFIG_HASH,
        "ledger_hash": ledger_hash or LEDGER_HASH,
    }
    events = []
    for step in (0, 1):
        own = dict(fields)
        if bad and bad[0] == step:
            own[bad[1]] = bad[2]
        events.append(
            ToolCall(
                arm=rc.arm,
                operator=rc.operator,
                step=step,
                agent="coder",
                model_id="m-sut",
                tool="write_file",
                args={"path": f"f{step}.py"},
                **own,
            )
        )
    for offset, (cls, content) in enumerate(
        zip((BoundaryOut, BoundaryIn), handoff or (), strict=False)
    ):
        events.append(
            cls(
                arm=rc.arm,
                operator=rc.operator,
                step=2 + offset,
                agent="planner",
                model_id="m-planner",
                boundary=rc.target_boundary,
                content=content,
                **fields,
            )
        )
    return events


class FakeTask:
    def __init__(self, id: str, constraint: str, boundary: str = "delegation:planner->coder"):
        self.id, self.constraint, self.boundary = id, constraint, boundary
        self.user_message = f"Do {id}."


class FakeApp:
    repo_root = "/repo"

    def __init__(self, behaviour: dict | None = None):
        self.behaviour = behaviour or {}
        self.graph: FakeGraph | None = None
        self.built: dict = {}
        self.success_calls: list[tuple] = []
        self.builds = 0

    def tasks(self):
        return [FakeTask("X1", "C-001"), FakeTask("X2", "C-002")]

    async def base_image(self, client) -> str:
        return BASE

    def build(self, backend, *, middleware, subagent_middleware, checkpointer):
        self.built = {"middleware": middleware, "subagent_middleware": subagent_middleware}
        self.builds += 1
        self.graph = FakeGraph(
            backend.session, middleware[0].run_config, checkpointer, self.behaviour, self.builds
        )
        return self.graph

    async def task_success(self, session, task):
        self.success_calls.append((str(session.uuid), task.id))
        return self.behaviour.get("success", (True, ""))


def constraint(cid: str) -> Constraint:
    return Constraint(
        id=cid,
        text=f"Rule {cid}.",
        kind="constraint",
        source="user",
        authority="binding",
        scope=("coder",),
        origin="declared",
        check=ToolForbidden(name="rm"),
    )


LEDGER_YAML = (ROOT / "nemogate.yaml").read_text()
LEDGER = Ledger(version=1, constraints=(constraint("C-001"), constraint("C-002")))
LEDGER_HASH = record.ledger_hash(LEDGER)
SAFE_CONFIG_HASH = load_config(ROOT / "config.yaml").safe_config.hash


def violation(cid: str, step: int) -> Violation:
    return Violation(constraint_id=cid, step=step, predicate="tool_forbidden", detail="d")


class Harness:
    """A Runner wired to fakes, plus handles to inspect what reached them."""

    def __init__(self, tmp_path: Path, behaviour: dict | None = None, **runner_kwargs):
        self.client = FakeClient()
        self.app = FakeApp(behaviour)
        self.runs_dir = tmp_path / "runs"
        self.evaluated: list[tuple] = []
        self.middleware_lists: list[list] = []
        self.backends: list[SimpleNamespace] = []
        kwargs = dict(
            app=self.app,
            evaluate=self._evaluate,
            make_middleware=self._make_middleware,
            ledger=LEDGER,
            config=load_config(ROOT / "config.yaml"),
            runs_dir=self.runs_dir,
            client_factory=lambda: self.client,
            backend_factory=self._backend,
            allow_dirty=True,
        )
        kwargs.update(runner_kwargs)
        self.runner = Runner(**kwargs)

    def _evaluate(self, ledger, trace, fs_diff):
        self.evaluated.append((ledger, trace, fs_diff))
        return [violation("C-002", 1), violation("C-001", 0), violation("C-002", 2)]

    def _make_middleware(self, run_config):
        if self.app.behaviour.get("spend_in_middleware"):
            append_spend(Role.helper, "m-helper", 7, 3, 0.04)
        mw = [FakeMiddleware(run_config)]
        self.middleware_lists.append(mw)
        return mw

    def _backend(self, session):
        backend = SimpleNamespace(session=session)
        self.backends.append(backend)
        return backend

    async def run_one(self, task_id="X1", arm=Arm.gated, operator=Operator.drop, seed=3):
        task = next(t for t in self.app.tasks() if t.id == task_id)
        return await self.runner.run_one(task, arm, operator, seed=seed, repo_ref="HEAD")

    def run_dir(self) -> Path:
        (only,) = list(self.runs_dir.iterdir())
        return only


def head_sha() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


# ---- run_one --------------------------------------------------------------------------------


def test_run_one_success_writes_every_file_and_a_complete_record(tmp_path):
    h = Harness(tmp_path)
    rec = asyncio.run(h.run_one("X1", Arm.gated, Operator.drop, seed=3))

    run_dir = h.run_dir()
    assert run_dir.name == rec.run_id
    for name in (
        "ledger.yaml",
        "run_config.json",
        "fs_diff.json",
        "violations.json",
        "carriage.json",
        "record.json",
        "trace.jsonl",
        "checkpoints.sqlite",
    ):
        assert (run_dir / name).is_file(), name
    assert not (run_dir / "task_check.txt").exists()
    assert not (run_dir / "error.txt").exists()

    config = load_config(ROOT / "config.yaml")
    assert rec.run_id.startswith("r-")
    assert (rec.task_id, rec.arm, rec.operator) == ("X1", Arm.gated, Operator.drop)
    assert (rec.constraint_id, rec.boundary) == ("C-001", "delegation:planner->coder")
    assert rec.violations == ("C-002", "C-001")  # unique, first-seen order
    assert rec.task_success is True
    assert rec.tokens == {Role.helper: 0, Role.sut: 150, Role.planner: 15}
    assert rec.cost_usd == pytest.approx(0.03)
    assert rec.contree_image == FINAL
    assert rec.checkpoint_id == "ckpt-7"
    assert rec.model_ids == {role: config.models[role].id for role in Role}
    assert rec.safe_config_hash == config.safe_config.hash
    assert rec.repo_sha == head_sha()
    assert rec.lockfile_hash == hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()[:12]
    assert RunRecord.model_validate_json((run_dir / "record.json").read_text()) == rec

    all_violations = json.loads((run_dir / "violations.json").read_text())
    assert [(v["constraint_id"], v["step"]) for v in all_violations] == [
        ("C-002", 1),
        ("C-001", 0),
        ("C-002", 2),
    ]
    fs_diff = json.loads((run_dir / "fs_diff.json").read_text())
    assert fs_diff == [
        {"path": "a.py", "op": "modified"},
        {"path": "b.py", "op": "deleted"},
        {"path": "c.py", "op": "added"},
    ]


def test_run_one_gives_the_gate_the_full_ledger_file_and_the_oracle_only_the_target(tmp_path):
    h = Harness(tmp_path)
    rec = asyncio.run(h.run_one("X2", Arm.mutated, Operator.hedge, seed=5))
    run_dir = h.run_dir()

    on_disk = load_ledger_yaml((run_dir / "ledger.yaml").read_text())
    assert on_disk == LEDGER  # the gate pins run_config.target_constraint from the full ledger

    (mw_list,) = h.middleware_lists
    rc = mw_list[0].run_config
    assert (rc.run_id, rc.task_id, rc.arm, rc.operator, rc.seed) == (
        rec.run_id,
        "X2",
        Arm.mutated,
        Operator.hedge,
        5,
    )
    assert (rc.target_constraint, rc.target_boundary) == ("C-002", "delegation:planner->coder")
    assert rc.repo_ref == "HEAD"
    assert rc.ledger_path == str(run_dir / "ledger.yaml")
    assert rc.trace_path == str(run_dir / "trace.jsonl")
    assert RunConfig.model_validate_json((run_dir / "run_config.json").read_text()) == rc

    ((ledger, trace, fs_diff),) = h.evaluated
    assert [c.id for c in ledger.constraints] == ["C-002"]
    assert trace == trace_events(rc)
    assert fs_diff == EXPECTED_DIFF

    scoped = record.scoped_ledger(LEDGER, "C-002")
    assert rec.ledger_hash == record.ledger_hash(LEDGER)
    assert rec.ledger_hash != record.ledger_hash(scoped)
    # the hash of the ledger file the gate loads is the hash the record carries
    assert record.ledger_hash(load_ledger_yaml(Path(rc.ledger_path).read_text())) == rec.ledger_hash


def test_run_one_takes_the_ledger_hash_from_the_injected_function_over_the_full_ledger(
    tmp_path,
):
    seen = []

    def fake_hash(ledger):
        seen.append(ledger)
        return "h-" + ",".join(c.id for c in ledger.constraints)

    h = Harness(tmp_path, {"trace_ledger_hash": "h-C-001,C-002"}, ledger_hash_fn=fake_hash)
    rec = asyncio.run(h.run_one("X1"))
    assert rec.ledger_hash == "h-C-001,C-002"
    assert seen == [LEDGER]


def test_run_one_gives_the_same_list_to_both_middleware_slots(tmp_path):
    """The gate's middleware, in order, then the boundary recorder; one list for both slots."""
    h = Harness(tmp_path)
    asyncio.run(h.run_one())
    (mw_list,) = h.middleware_lists
    stack = h.app.built["middleware"]
    assert h.app.built["subagent_middleware"] is stack
    assert len(stack) == len(mw_list) + 1
    assert all(got is want for got, want in zip(stack, mw_list, strict=False))
    assert type(stack[-1]).__name__ == "BoundaryRecorder"


def test_run_one_invokes_the_graph_with_the_user_message_thread_and_recursion_limit(tmp_path):
    h = Harness(tmp_path, recursion_limit=25)
    rec = asyncio.run(h.run_one("X1"))
    graph = h.app.graph
    expected_config = {"configurable": {"thread_id": rec.run_id}, "recursion_limit": 25}
    assert graph.invoked == [
        ({"messages": [{"role": "user", "content": "Do X1."}]}, expected_config)
    ]
    assert graph.state_configs == [expected_config]


def test_run_one_starts_the_session_from_the_base_image_and_diffs_base_against_final(
    tmp_path,
):
    h = Harness(tmp_path)
    asyncio.run(h.run_one())
    # the session is built on BASE; the snapshots then read BASE and FINAL as plain images
    assert h.client.used == [UUID(BASE), UUID(BASE), UUID(FINAL)]
    (session,) = h.client.sessions
    assert h.backends[0].session is session
    assert h.app.success_calls == [(FINAL, "X1")]


def test_run_one_closes_the_checkpoint_database_after_the_run(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.run_one())
    saver = h.app.graph.checkpointer
    assert type(saver).__name__ == "AsyncSqliteSaver"
    with pytest.raises(ValueError):  # aiosqlite: "no active connection" once closed
        asyncio.run(saver.conn.execute("select 1"))


def test_run_one_keeps_the_tail_of_a_failing_task_check(tmp_path):
    h = Harness(tmp_path, {"success": (False, "AssertionError: nope")})
    rec = asyncio.run(h.run_one())
    assert rec.task_success is False
    assert (h.run_dir() / "task_check.txt").read_text() == "AssertionError: nope"


def test_run_one_without_a_trace_file_fails_naming_the_path_and_writes_no_record(tmp_path):
    h = Harness(tmp_path, {"write_trace": False})
    with pytest.raises(RuntimeError, match=re.escape(str(h.runs_dir))) as info:
        asyncio.run(h.run_one())
    assert "trace.jsonl" in str(info.value)
    assert h.evaluated == []
    assert not (h.run_dir() / "record.json").exists()


def test_run_one_records_a_crash_in_error_txt_and_reraises(tmp_path):
    h = Harness(tmp_path, {"raises": "boom"})
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(h.run_one())
    run_dir = h.run_dir()
    error = (run_dir / "error.txt").read_text()
    assert "Traceback" in error
    assert "ValueError: boom" in error
    assert not (run_dir / "record.json").exists()
    assert h.evaluated == []


def test_run_one_refuses_a_ref_that_is_not_the_checkout(tmp_path):
    h = Harness(tmp_path)
    task = h.app.tasks()[0]
    with pytest.raises(ValueError, match="checkout"):
        asyncio.run(h.runner.run_one(task, Arm.gated, Operator.drop, seed=0, repo_ref="HEAD~1"))
    assert not h.runs_dir.exists()


def test_run_one_with_an_empty_trace_file_fails_and_writes_no_record(tmp_path):
    h = Harness(tmp_path, {"trace_text": "\n"})
    with pytest.raises(RuntimeError, match="no events") as info:
        asyncio.run(h.run_one())
    assert "trace.jsonl" in str(info.value)
    assert h.evaluated == []
    assert not (h.run_dir() / "record.json").exists()


@pytest.mark.parametrize(
    ("field", "wrong", "expected"),
    [
        ("run_id", "r-other", None),
        ("ledger_hash", "000000000000", LEDGER_HASH),
        ("safe_config_hash", "ffffffffffff", SAFE_CONFIG_HASH),
    ],
)
def test_run_one_rejects_a_trace_whose_hashes_or_run_id_differ_from_the_record(
    tmp_path, field, wrong, expected
):
    h = Harness(tmp_path, {"bad_event": (1, field, wrong)})
    with pytest.raises(RuntimeError) as info:
        asyncio.run(h.run_one())
    message = str(info.value)
    assert field in message
    assert repr(wrong) in message
    assert repr(expected or h.run_dir().name) in message
    assert "step 1" in message
    assert h.evaluated == []
    assert not (h.run_dir() / "record.json").exists()


def test_run_one_charges_a_model_call_made_while_building_the_gate_to_the_run(tmp_path):
    h = Harness(tmp_path, {"spend_in_middleware": True})
    rec = asyncio.run(h.run_one())
    assert rec.tokens == {Role.helper: 10, Role.sut: 150, Role.planner: 15}
    assert rec.cost_usd == pytest.approx(0.07)


# ---- default client -------------------------------------------------------------------------


def _fake_contree(monkeypatch, seen: list):
    class Contree:
        def __init__(self):
            seen.append((os.environ.get("NEBIUS_API_KEY"), os.environ.get("NEBIUS_PROJECT_ID")))

    monkeypatch.setitem(sys.modules, "contree_sdk", _module("contree_sdk", Contree=Contree))


def test_default_client_loads_the_env_file_before_constructing_contree(monkeypatch):
    def fake_load_dotenv(path=None):  # stands for a `.env` that holds both variables
        monkeypatch.setenv("NEBIUS_API_KEY", "k-from-file")
        monkeypatch.setenv("NEBIUS_PROJECT_ID", "p-from-file")

    monkeypatch.delenv("NEBIUS_PROJECT_ID", raising=False)
    monkeypatch.setattr(env, "load_dotenv", fake_load_dotenv)
    seen: list = []
    _fake_contree(monkeypatch, seen)
    runner._default_client()
    assert seen == [("k-from-file", "p-from-file")]


def test_default_client_refuses_missing_credentials_without_constructing_contree(monkeypatch):
    monkeypatch.setattr(env, "load_dotenv", lambda path=None: None)  # never read a real `.env`
    monkeypatch.delenv("NEBIUS_PROJECT_ID", raising=False)
    seen: list = []
    _fake_contree(monkeypatch, seen)
    with pytest.raises(env.MissingEnvError, match="NEBIUS_API_KEY.*NEBIUS_PROJECT_ID"):
        runner._default_client()
    assert seen == []


# ---- arun / run -----------------------------------------------------------------------------


def stub_run_one(h: Harness, calls: list, fail_on: int | None = None):
    async def run_one(task, arm, operator, *, seed, repo_ref="main"):
        calls.append((task.id, arm, operator, seed, repo_ref))
        if fail_on == len(calls):
            raise RuntimeError("stop here")
        return SimpleNamespace(call=len(calls))

    h.runner.run_one = run_one


def test_arun_runs_tasks_then_pairs_then_repetitions_with_seeds_from_zero(tmp_path):
    h = Harness(tmp_path)
    calls: list = []
    stub_run_one(h, calls)
    out = asyncio.run(
        h.runner.arun(
            ["X2", "X1"], [Arm.natural, Arm.gated], [Operator.none, Operator.drop], 2, "HEAD"
        )
    )
    expected = []
    for task in ("X2", "X1"):
        for arm, op in ((Arm.natural, Operator.none), (Arm.gated, Operator.drop)):
            for seed in (0, 1):
                expected.append((task, arm, op, seed, "HEAD"))
    assert calls == expected
    assert [r.call for r in out] == list(range(1, 9))


def test_arun_with_an_unknown_task_id_raises_keyerror_before_any_run(tmp_path):
    h = Harness(tmp_path)
    calls: list = []
    stub_run_one(h, calls)
    with pytest.raises(KeyError, match="nope"):
        asyncio.run(h.runner.arun(["X1", "nope"], [Arm.natural], [Operator.none], 1))
    assert calls == []


def test_arun_stops_at_the_first_error(tmp_path):
    h = Harness(tmp_path)
    calls: list = []
    stub_run_one(h, calls, fail_on=2)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(h.runner.arun(["X1", "X2"], [Arm.natural], [Operator.none], 2))
    assert [c[:4] for c in calls] == [
        ("X1", Arm.natural, Operator.none, 0),
        ("X1", Arm.natural, Operator.none, 1),
    ]


def test_run_is_the_sync_wrapper_of_arun(tmp_path):
    h = Harness(tmp_path)
    calls: list = []
    stub_run_one(h, calls)
    out = h.runner.run(["X1"], [Arm.gated], [Operator.drop], 1)
    assert calls == [("X1", Arm.gated, Operator.drop, 0, "main")]
    assert len(out) == 1


# ---- afork / fork ---------------------------------------------------------------------------


def stub_fork_one(h: Harness, calls: list, fail_on: int | None = None):
    async def fork_one(source_run_id, arm, operator, *, seed, repo_ref="main"):
        calls.append((source_run_id, arm, operator, seed, repo_ref))
        if fail_on == len(calls):
            raise RuntimeError("stop here")
        return SimpleNamespace(call=len(calls))

    h.runner.fork_one = fork_one


def test_afork_runs_pairs_then_repetitions_from_the_one_source_run(tmp_path):
    """Catches the arms runner skipping a pair, pairing natural with an operator, or reusing
    one seed across repetitions."""
    h = Harness(tmp_path)
    calls: list = []
    stub_fork_one(h, calls)
    out = asyncio.run(
        h.runner.afork("r-src", [Arm.natural, Arm.gated], [Operator.none, Operator.drop], 2, "HEAD")
    )
    assert calls == [
        ("r-src", Arm.natural, Operator.none, 0, "HEAD"),
        ("r-src", Arm.natural, Operator.none, 1, "HEAD"),
        ("r-src", Arm.gated, Operator.drop, 0, "HEAD"),
        ("r-src", Arm.gated, Operator.drop, 1, "HEAD"),
    ]
    assert [r.call for r in out] == [1, 2, 3, 4]


def test_afork_stops_at_the_first_error(tmp_path):
    h = Harness(tmp_path)
    calls: list = []
    stub_fork_one(h, calls, fail_on=1)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(h.runner.afork("r-src", [Arm.mutated], [Operator.drop], 3))
    assert len(calls) == 1


def test_fork_is_the_sync_wrapper_of_afork(tmp_path):
    h = Harness(tmp_path)
    calls: list = []
    stub_fork_one(h, calls)
    out = h.runner.fork("r-src", [Arm.mutated], [Operator.hedge], 1)
    assert calls == [("r-src", Arm.mutated, Operator.hedge, 0, "main")]
    assert len(out) == 1


# ---- default run() --------------------------------------------------------------------------


def _module(name: str, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def test_default_run_names_oracle_when_it_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "oracle", None)  # makes `import oracle` fail
    with pytest.raises(RuntimeError, match="oracle"):
        runner.run(["X1"], [Arm.natural], [Operator.none], 1)


def test_default_run_names_gate_when_it_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "oracle", _module("oracle", evaluate=lambda *a: []))
    monkeypatch.setitem(sys.modules, "gate", None)
    with pytest.raises(RuntimeError, match="gate"):
        runner.run(["X1"], [Arm.natural], [Operator.none], 1)


def test_default_run_names_the_missing_ledger_file(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "oracle", _module("oracle", evaluate=lambda *a: []))
    monkeypatch.setitem(sys.modules, "gate", _module("gate", make_middleware=lambda rc: []))
    monkeypatch.setattr(runner, "LEDGER_PATH", tmp_path / "no-such-ledger.yaml")
    with pytest.raises(RuntimeError, match="no-such-ledger.yaml"):
        runner.run(["X1"], [Arm.natural], [Operator.none], 1)


# ---- CLI ------------------------------------------------------------------------------------

CLI_ARGS = [
    "--tasks", "X1", "X2",
    "--arms", "natural", "gated",
    "--operators", "none", "drop",
    "--n", "3",
]  # fmt: skip


def _no_run(*args, **kwargs):
    raise AssertionError("run() must not be called")


def test_cli_refuses_without_the_live_flag(monkeypatch, capsys):
    monkeypatch.delenv("NEMOGATE_LIVE", raising=False)
    monkeypatch.setattr(runner, "run", _no_run)
    assert runner.main([*CLI_ARGS, "--yes"]) == 2
    assert "NEMOGATE_LIVE" in capsys.readouterr().err


def test_cli_without_yes_prints_the_run_count_and_points_at_the_budget_check(monkeypatch, capsys):
    monkeypatch.setattr(runner, "is_live", lambda: True)  # the test never sets NEMOGATE_LIVE
    monkeypatch.setattr(runner, "run", _no_run)
    assert runner.main(CLI_ARGS) == 1
    out = capsys.readouterr()
    # 2 tasks x 2 pairs ((natural, none), (gated, drop)) x 3 repetitions
    assert "planned runs: 12" in out.out
    assert "python -m replay.budget" in out.err + out.out


def test_cli_with_yes_runs_and_prints_one_line_per_record(monkeypatch, capsys):
    monkeypatch.setattr(runner, "is_live", lambda: True)
    got = []

    class FakeDefaultRunner:
        errors: list = []

        def run(self, task_ids, arms, operators, n, repo_ref):
            got.append((task_ids, arms, operators, n, repo_ref))
            return fake_records()

    def fake_records():
        return [
            RunRecord(
                run_id="r-1",
                task_id="X1",
                arm=Arm.gated,
                operator=Operator.drop,
                constraint_id="C-001",
                boundary="delegation:planner->coder",
                violations=("C-002", "C-001"),
                task_success=True,
                tokens={Role.helper: 0, Role.sut: 1, Role.planner: 2},
                cost_usd=0.03,
                checkpoint_id=None,
                contree_image=None,
                model_ids={Role.helper: "a", Role.sut: "b", Role.planner: "c"},
                safe_config_hash="s",
                ledger_hash="l",
                repo_sha="x",
                lockfile_hash="k",
            )
        ]

    monkeypatch.setattr(runner, "default_runner", lambda runs_dir=None: FakeDefaultRunner())
    assert runner.main([*CLI_ARGS, "--repo-ref", "v1", "--yes"]) == 0
    assert got == [
        (["X1", "X2"], [Arm.natural, Arm.gated], [Operator.none, Operator.drop], 3, "v1")
    ]
    lines = capsys.readouterr().out.splitlines()
    assert (
        "r-1 task=X1 arm=gated operator=drop violations=C-002,C-001 task_success=True "
        "cost_usd=0.0300"
    ) in lines


def test_cli_runs_dir_sends_the_run_folders_to_that_directory(monkeypatch, tmp_path):
    # Breaks if --runs-dir is ignored: compaction runs would land in runs/ beside ordinary
    # natural runs and be pooled with them.
    monkeypatch.setattr(runner, "is_live", lambda: True)
    monkeypatch.setattr(runner, "run", _no_run)
    seen = []

    class FakeDefaultRunner:
        errors: list = []

        def run(self, *args):
            seen.append(args)
            return []

    def fake_default_runner(runs_dir):
        seen.append(runs_dir)
        return FakeDefaultRunner()

    monkeypatch.setattr(runner, "default_runner", fake_default_runner)
    target = tmp_path / "compaction"
    assert runner.main([*CLI_ARGS, "--runs-dir", str(target), "--yes"]) == 0
    assert seen[0] == target
    assert seen[1][3] == 3


def git_repo_ignoring_runs(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("runs/\n")
    return repo


def test_runs_dir_inside_the_checkout_must_be_git_ignored(tmp_path):
    # Breaks if a runs dir that git would see as untracked files is accepted: the first run
    # then makes the checkout dirty and the second fails require_checkout_at after spending.
    repo = git_repo_ignoring_runs(tmp_path)
    runner.require_ignored_runs_dir(repo / "runs" / "compaction", repo)  # ignored: fine
    runner.require_ignored_runs_dir(tmp_path / "elsewhere", repo)  # outside the checkout: fine
    with pytest.raises(ValueError, match="not git-ignored"):
        runner.require_ignored_runs_dir(repo / "out", repo)


def test_cli_refuses_an_unignored_runs_dir_before_anything_runs(monkeypatch, capsys):
    # Breaks if the check happens only after the first (paid) run.
    monkeypatch.setattr(runner, "is_live", lambda: True)
    monkeypatch.setattr(runner, "run", _no_run)
    monkeypatch.setattr(runner, "default_runner", _no_run)
    unignored = runner.ROOT / "out-not-ignored"
    assert runner.main([*CLI_ARGS, "--runs-dir", str(unignored), "--yes"]) == 2
    assert "not git-ignored" in capsys.readouterr().err


def test_default_runner_writes_to_runs_dir_unless_told_otherwise(monkeypatch, tmp_path):
    # Breaks if default_runner() stops defaulting to runs/ or ignores the directory it is given.
    ledger = tmp_path / "nemogate.yaml"
    ledger.write_text(LEDGER_YAML)
    monkeypatch.setitem(sys.modules, "oracle", _module("oracle", evaluate=lambda *a: []))
    monkeypatch.setitem(sys.modules, "gate", _module("gate", make_middleware=lambda rc: []))
    monkeypatch.setattr(runner, "LEDGER_PATH", ledger)
    assert runner.default_runner().runs_dir == runner.RUNS_DIR
    assert runner.default_runner(tmp_path / "other").runs_dir == tmp_path / "other"


# ---- the agents' sandbox ----------------------------------------------------------------------


class RecordingSession:
    """A session that records where each command ran; stands in for a ContreeSession."""

    uuid = UUID(BASE)

    def __init__(self):
        self.cwds: list[str | None] = []

    async def run(self, *, shell, timeout, disposable, truncate_output_at, cwd=None):
        self.cwds.append(cwd)
        result = SimpleNamespace(stdout="", stderr="", exit_code=0, truncated=False)
        return SimpleNamespace(result=result)


def test_default_backend_runs_agent_commands_from_the_apps_repo_root(tmp_path, monkeypatch):
    """Catches the agents' sandbox starting in the image default directory, not the repo."""
    monkeypatch.setattr("contree_sdk.langchain.sandbox.ContreeSession", RecordingSession)
    h = Harness(tmp_path, backend_factory=None)
    h.app.repo_root = "/srv/checkout"
    session = RecordingSession()

    backend = h.runner.backend_factory(session)
    asyncio.run(backend.aexecute("grep -rHnF needle ."))

    assert session.cwds == ["/srv/checkout"]


def test_an_explicit_backend_factory_is_called_with_the_session_only(tmp_path):
    """Catches the cwd change breaking callers whose factory takes one argument."""
    h = Harness(tmp_path)
    session = FakeSession(BASE)

    assert h.runner.backend_factory(session).session is session
    assert [b.session for b in h.backends] == [session]


# ---- carriage.json ----------------------------------------------------------------------------


def run_natural(tmp_path, behaviour, monkeypatch, env=None):
    """One natural run (operator none) of a task whose user message states the `rm` rule."""
    for name in ("NEMOGATE_COMPACT_TOKENS", "NEMOGATE_COMPACT_KEEP"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    h = Harness(tmp_path, behaviour)
    task = FakeTask("X1", "C-001")
    task.user_message = "Add a flag. Never run rm in the repo."
    asyncio.run(h.runner.run_one(task, Arm.natural, Operator.none, seed=0, repo_ref="HEAD"))
    return json.loads((h.run_dir() / "carriage.json").read_text())


def test_carriage_json_labels_a_natural_drop_and_records_the_knob(tmp_path, monkeypatch):
    # Breaks if the runner does not classify the run from its trace and the oracle's verdict, or
    # drops the compaction knob values, which nothing else hashes.
    carriage = run_natural(
        tmp_path,
        {"handoff": ("Add the flag.", "Add the flag.")},
        monkeypatch,
        env={"NEMOGATE_COMPACT_TOKENS": "8000"},
    )
    assert carriage == {
        "classifier_version": 4,
        "label": "DROPPED",
        "reason": "anchor_mentioned=False carried=False violated=True",
        "anchors": ["rm"],
        "in_planner": True,
        "anchor_mentioned": False,
        "carried": False,
        "violated": True,
        "natural_guard": True,
        "evidence": {
            "user_sentence": "Never run rm in the repo.",
            "delegation_excerpt": "Add the flag.",
            "cue": None,
            "delegations_checked": 1,
            "delegations": ["Add the flag."],
            "carried_by": None,
            "lost_at": None,
        },
        "compaction": None,
        "knobs": {"NEMOGATE_COMPACT_TOKENS": "8000", "NEMOGATE_COMPACT_KEEP": None},
    }


def test_carriage_json_carries_the_compaction_evidence_of_the_planner(tmp_path, monkeypatch):
    # Breaks if the runner does not read the summarization event from the graph state, or loses
    # whether it was written before the delegating checkpoint.
    event = {
        "cutoff_index": 4,
        "summary_message": AIMessage(content="The user wants a flag."),
        "file_path": None,
    }
    carriage = run_natural(
        tmp_path,
        {"handoff": ("Add the flag.", "Add the flag."), "summarization_event": event},
        monkeypatch,
        env={"NEMOGATE_COMPACT_TOKENS": "8000", "NEMOGATE_COMPACT_KEEP": "2"},
    )
    assert carriage["compaction"] == {
        "fired": True,
        "cutoff_index": 4,
        "anchor_in_summary": False,
        "before_delegation": True,
        "delegations": [{"fired": True, "cutoff_index": 4, "anchor_in_summary": False}],
    }
    assert carriage["knobs"] == {"NEMOGATE_COMPACT_TOKENS": "8000", "NEMOGATE_COMPACT_KEEP": "2"}


def test_carriage_json_is_unknown_when_nothing_was_delegated(tmp_path, monkeypatch):
    # Breaks if a run with no handoff at the target boundary gets a carried or dropped label.
    carriage = run_natural(tmp_path, {}, monkeypatch)
    assert carriage["label"] == "UNKNOWN"
    assert carriage["knobs"] == {"NEMOGATE_COMPACT_TOKENS": None, "NEMOGATE_COMPACT_KEEP": None}


# ---- timing.json ------------------------------------------------------------------------------


def fake_clock(monkeypatch, *stamps):
    """_utcnow returns these datetimes in order: the start of the run, then its end."""
    times = iter(stamps)
    monkeypatch.setattr(runner, "_utcnow", lambda: next(times))


def test_a_finished_run_writes_timing_json_with_start_end_and_seconds(tmp_path, monkeypatch):
    # Breaks if wall time is not recorded per run, or is recorded as the wrong span.
    fake_clock(
        monkeypatch,
        datetime(2026, 10, 16, 13, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 16, 13, 2, 17, 500000, tzinfo=UTC),
    )
    h = Harness(tmp_path)
    asyncio.run(h.run_one("X1", Arm.gated, Operator.drop, seed=3))
    assert json.loads((h.run_dir() / "timing.json").read_text()) == {
        "start": "2026-10-16T13:00:00+00:00",
        "end": "2026-10-16T13:02:17.500000+00:00",
        "seconds": 137.5,
    }


def test_a_crashed_run_also_gets_timing_json(tmp_path, monkeypatch):
    # Breaks if an errored run's time is lost: it was spent like any other.
    fake_clock(
        monkeypatch,
        datetime(2026, 10, 16, 13, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 16, 13, 0, 42, tzinfo=UTC),
    )
    h = Harness(tmp_path, {"raises": "boom"})
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(h.run_one("X1", Arm.gated, Operator.drop, seed=3))
    assert json.loads((h.run_dir() / "timing.json").read_text())["seconds"] == 42.0


# ---- a sweep carries on past a crashed run ----------------------------------------------------


def sweep_three(h: Harness):
    """Three natural runs of one task, all on the fake gate, app and Contree."""
    return h.runner.run(["X1"], [Arm.natural], [Operator.none], 3, "HEAD")


def error_folders(h: Harness) -> list[Path]:
    return sorted(p for p in h.runs_dir.iterdir() if (p / "error.json").exists())


def test_arun_carries_on_past_a_run_that_raises_and_writes_error_json(tmp_path):
    # Breaks if one crashed run still stops the sweep, leaves no error.json, or loses the
    # conditions (reproducibility fields) a top-up needs to attribute the attempt.
    h = Harness(tmp_path, {"raises_on": {2}})
    records = sweep_three(h)

    assert len(records) == 2
    assert h.app.builds == 3  # the third run still started
    (bad,) = error_folders(h)
    assert not (bad / "record.json").exists()
    assert sorted(p.name for p in h.runs_dir.iterdir()) == sorted(
        [bad.name, *(r.run_id for r in records)]
    )
    data = json.loads((bad / "error.json").read_text())
    config = load_config(ROOT / "config.yaml")
    assert data == {
        "run_id": bad.name,
        "task_id": "X1",
        "arm": "natural",
        "operator": "none",
        "constraint_id": "C-001",
        "boundary": "delegation:planner->coder",
        "model_ids": {role.value: config.models[role].id for role in Role},
        "safe_config_hash": SAFE_CONFIG_HASH,
        "ledger_hash": LEDGER_HASH,
        "repo_sha": head_sha(),
        "lockfile_hash": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()[:12],
        "cost_usd": 0.0,
        "error_type": "builtins.ValueError",
        "message": "boom in run 2",
        "traceback_tail": data["traceback_tail"],
    }
    tail = data["traceback_tail"].splitlines()
    assert tail[-1] == "ValueError: boom in run 2"
    assert len(tail) <= 20
    assert [(e.run_id, e.error_type) for e in h.runner.errors] == [
        (bad.name, "builtins.ValueError")
    ]
    assert h.runner.records == records


def test_error_json_keeps_only_the_last_twenty_traceback_lines(tmp_path):
    # Breaks if a deep traceback is written whole, or cut from the wrong end.
    h = Harness(tmp_path)
    error = ValueError("deep")
    text = "".join(f"frame {i}\n" for i in range(100)) + "ValueError: deep\n"
    task = h.app.tasks()[0]
    run_dir = h.runs_dir / "r-x"
    run_dir.mkdir(parents=True)
    repro = h.runner.reproducibility("HEAD")
    runner._write_error(run_dir, task, Arm.natural, Operator.none, repro, error, text)
    tail = json.loads((run_dir / "error.json").read_text())["traceback_tail"].splitlines()
    assert tail == [*(f"frame {i}" for i in range(81, 100)), "ValueError: deep"]


def test_error_json_records_what_the_crashed_run_spent(tmp_path):
    # Breaks if a run that raised after spending leaves no trace of the money: error.json must
    # carry the run's spend-log total (0.04 helper spend made while the gate was built).
    h = Harness(tmp_path, {"raises": "boom", "spend_in_middleware": True})
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(h.run_one("X1", Arm.gated, Operator.drop, seed=3))
    assert json.loads((h.run_dir() / "error.json").read_text())["cost_usd"] == pytest.approx(0.04)


def test_a_failure_before_the_run_folder_exists_stops_the_sweep_and_keeps_the_traceback(
    tmp_path, monkeypatch
):
    # Breaks if a failure before the folder exists loses the traceback (the wrapper must create
    # the folder) or is turned into an attempt.
    h = Harness(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(record, "write_ledger", fail)
    with pytest.raises(OSError, match="disk full"):
        sweep_three(h)
    assert "OSError: disk full" in (h.run_dir() / "error.txt").read_text()
    assert error_folders(h) == []
    assert h.app.builds == 0


def _raise_value_error(*args, **kwargs):
    raise ValueError("refused during setup")


@pytest.mark.parametrize(
    "broken",
    [
        {"make_middleware": _raise_value_error},
        {"client_factory": _raise_value_error},
        {"backend_factory": _raise_value_error},
    ],
    ids=["gate", "sandbox-client", "backend"],
)
def test_arun_stops_on_any_failure_during_setup_whatever_its_type(tmp_path, broken):
    # Breaks if a plain ValueError raised while the run is being set up (the gate refusing, the
    # sandbox not starting) is recorded as an attempt: every run would fail the same way, and a
    # top-up would count them and plan nothing.
    h = Harness(tmp_path, **broken)
    with pytest.raises(ValueError, match="refused during setup"):
        sweep_three(h)
    assert error_folders(h) == []
    assert h.runner.errors == []
    assert len(list(h.runs_dir.iterdir())) == 1  # run 2 never started


def test_arun_stops_when_the_real_gate_refuses_a_stale_safe_config_hash(tmp_path, monkeypatch):
    # The real gate.make_middleware raises ValueError (not ConfigError) for a stale hash.
    # Breaks if that is carried past: three error.json attempts for n=3 and nothing measured.
    import gate

    monkeypatch.setattr(gate, "safe_config_hash", lambda config: "stale0000000")
    h = Harness(tmp_path, make_middleware=gate.make_middleware)
    with pytest.raises(ValueError, match="stale"):
        sweep_three(h)
    assert error_folders(h) == []
    assert h.app.builds == 0


def test_arun_records_a_failure_after_the_agents_ran_as_an_attempt(tmp_path):
    # Breaks if the setup/execution line sits too early: a run whose gate never wrote a trace
    # (found only after the agents ran) is an outcome of that run, not a broken sweep.
    h = Harness(tmp_path, {"write_trace": False})
    assert h.runner.run(["X1"], [Arm.natural], [Operator.none], 2, "HEAD") == []
    assert len(error_folders(h)) == 2
    assert [e.error_type for e in h.runner.errors] == ["builtins.RuntimeError"] * 2


@pytest.mark.parametrize(
    "stop",
    [
        SpendCapReached("Spend cap reached"),
        ConfigError("config.yaml leaves a value null"),
        MissingEnvError("NEBIUS_API_KEY is unset"),
    ],
    ids=["spend-cap", "config", "missing-env"],
)
def test_arun_stops_for_a_failure_no_later_run_can_avoid_and_writes_no_error_json(tmp_path, stop):
    # Breaks if a spend-cap refusal (AGENTS rule 3: stop and tell a human) or a config failure
    # is swallowed like an ordinary crash, so the sweep keeps going or counts it as an attempt.
    h = Harness(tmp_path, {"stops_on": {2: stop}})
    with pytest.raises(type(stop)):
        sweep_three(h)
    assert h.app.builds == 2  # run 3 never started
    assert error_folders(h) == []
    assert len(h.runner.records) == 1 and h.runner.errors == []
    assert len(list(h.runs_dir.iterdir())) == 2  # the finished run and the stopped one


def test_arun_stops_when_the_spend_cap_is_only_the_cause_of_the_error(tmp_path):
    # Breaks if a library that wraps the cap error (raise ... from cap) turns it into an
    # ordinary crash that the sweep carries on past.
    try:
        raise SpendCapReached("Spend cap reached")
    except SpendCapReached as cap:
        wrapped = RuntimeError("tool node failed")
        wrapped.__cause__ = cap
    h = Harness(tmp_path, {"stops_on": {1: wrapped}})
    with pytest.raises(RuntimeError, match="tool node failed"):
        sweep_three(h)
    assert h.app.builds == 1
    assert error_folders(h) == []


def test_arun_stops_for_an_error_before_a_run_begins_and_writes_nothing(tmp_path):
    # Breaks if a stale or dirty checkout is recorded as a failed attempt run after run.
    h = Harness(tmp_path)
    with pytest.raises(ValueError, match="checkout"):
        h.runner.run(["X1"], [Arm.natural], [Operator.none], 3, "HEAD~1")
    assert not h.runs_dir.exists()
    assert h.runner.errors == []


def test_a_single_run_one_that_raises_still_raises_and_leaves_error_json(tmp_path):
    h = Harness(tmp_path, {"raises": "boom"})
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(h.run_one())
    assert json.loads((h.run_dir() / "error.json").read_text())["message"] == "boom"


def cli_with(h: Harness, monkeypatch, *args: str) -> int:
    monkeypatch.setattr(runner, "is_live", lambda: True)
    monkeypatch.setattr(runner, "default_runner", lambda runs_dir=None: h.runner)
    return runner.main(["--arms", "natural", "--operators", "none", "--repo-ref", "HEAD", *args])


def test_cli_prints_the_error_summary_and_exits_1_when_a_run_errored(tmp_path, monkeypatch, capsys):
    # Breaks if a sweep with a crashed run exits 0 (a script would call it clean) or hides
    # which runs failed and why.
    h = Harness(tmp_path, {"raises_on": {2}})
    assert cli_with(h, monkeypatch, "--tasks", "X1", "--n", "3", "--yes") == 1
    (bad,) = error_folders(h)
    lines = capsys.readouterr().out.splitlines()
    assert "runs: 2 ok, 1 errored" in lines
    assert (
        f"errored {bad.name} task=X1 arm=natural operator=none error_type=builtins.ValueError"
        in (lines)
    )


def test_cli_exits_0_when_every_run_finished(tmp_path, monkeypatch, capsys):
    h = Harness(tmp_path)
    assert cli_with(h, monkeypatch, "--tasks", "X1", "--n", "2", "--yes") == 0
    assert "runs: 2 ok, 0 errored" in capsys.readouterr().out.splitlines()


def test_cli_reports_a_spend_cap_stop_and_exits_3(tmp_path, monkeypatch, capsys):
    # Breaks if the cap hit ends in a bare traceback with no word on what finished first.
    h = Harness(tmp_path, {"stops_on": {2: SpendCapReached("Spend cap reached: $1 spent")}})
    assert cli_with(h, monkeypatch, "--tasks", "X1", "--n", "3", "--yes") == 3
    out = capsys.readouterr()
    assert "stopped: Spend cap reached: $1 spent" in out.err
    assert "runs: 1 ok, 0 errored before the stop" in out.out.splitlines()


# ---- --top-up ---------------------------------------------------------------------------------


def run_dirs_by_cell(h: Harness) -> dict[tuple[str, int], int]:
    """(task id, seed) -> number of run folders holding that run config."""
    counts: dict[tuple[str, int], int] = {}
    for folder in h.runs_dir.iterdir():
        config = json.loads((folder / "run_config.json").read_text())
        key = (config["task_id"], config["seed"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def test_cli_top_up_runs_only_the_shortfall_and_counts_errored_runs_as_attempts(
    tmp_path, monkeypatch, capsys
):
    # Breaks if top-up reruns full cells, retries an errored run (biasing the sample), or
    # restarts seeds at 0 so the new runs repeat the old ones' seeds.
    h = Harness(tmp_path, {"raises_on": {2}})
    h.runner.run(["X1"], [Arm.natural], [Operator.none], 3, "HEAD")  # 2 records + 1 error
    capsys.readouterr()

    assert cli_with(h, monkeypatch, "--tasks", "X1", "X2", "--n", "4", "--top-up") == 1  # no --yes
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "X1 natural none: existing 3, planned 1",
        "X2 natural none: existing 0, planned 4",
        "planned runs: 5",
    ]
    assert h.app.builds == 3  # planning ran nothing

    h.app.behaviour = {}
    assert cli_with(h, monkeypatch, "--tasks", "X1", "X2", "--n", "4", "--top-up", "--yes") == 0
    assert h.app.builds == 3 + 5
    assert run_dirs_by_cell(h) == {
        ("X1", 0): 1,
        ("X1", 1): 1,
        ("X1", 2): 1,
        ("X1", 3): 1,  # the one new X1 run: seeds continue after the three attempts
        ("X2", 0): 1,
        ("X2", 1): 1,
        ("X2", 2): 1,
        ("X2", 3): 1,
    }
    assert "runs: 5 ok, 0 errored" in capsys.readouterr().out.splitlines()


def test_cli_top_up_of_a_full_cell_plans_nothing_and_runs_nothing(tmp_path, monkeypatch, capsys):
    h = Harness(tmp_path)
    h.runner.run(["X1"], [Arm.natural], [Operator.none], 2, "HEAD")
    capsys.readouterr()
    assert cli_with(h, monkeypatch, "--tasks", "X1", "--n", "2", "--top-up", "--yes") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["X1 natural none: existing 2, planned 0", "planned runs: 0"]
    assert h.app.builds == 2


def test_cli_top_up_ignores_runs_made_under_other_reproducibility_fields(
    tmp_path, monkeypatch, capsys
):
    # Breaks if runs from another commit are counted, so a new commit's sweep looks complete.
    h = Harness(tmp_path)
    (first,) = [r.run_id for r in h.runner.run(["X1"], [Arm.natural], [Operator.none], 1, "HEAD")]
    path = h.runs_dir / first / "record.json"
    data = json.loads(path.read_text())
    data["repo_sha"] = "0" * 40
    path.write_text(json.dumps(data))
    capsys.readouterr()
    assert cli_with(h, monkeypatch, "--tasks", "X1", "--n", "1", "--top-up") == 1
    assert capsys.readouterr().out.splitlines()[0] == "X1 natural none: existing 0, planned 1"


def test_cli_top_up_refuses_a_checkout_that_is_not_the_ref(tmp_path, monkeypatch, capsys):
    h = Harness(tmp_path)
    monkeypatch.setattr(runner, "is_live", lambda: True)
    monkeypatch.setattr(runner, "default_runner", lambda runs_dir=None: h.runner)
    code = runner.main(
        ["--tasks", "X1", "--arms", "natural", "--operators", "none", "--n", "1", "--top-up"]
        + ["--repo-ref", "HEAD~1", "--yes"]
    )
    assert code == 2
    assert "checkout" in capsys.readouterr().err
    assert h.app.builds == 0


def test_cli_top_up_never_repeats_a_seed_when_the_earlier_seeds_are_not_contiguous(
    tmp_path, monkeypatch
):
    # Breaks if the next seed is the attempt count: one attempt with seed 3 and n=4 would give
    # the new runs seeds 1, 2, 3 and repeat seed 3.
    h = Harness(tmp_path)
    task = h.app.tasks()[0]
    asyncio.run(h.runner.run_one(task, Arm.natural, Operator.none, seed=3, repo_ref="HEAD"))
    assert cli_with(h, monkeypatch, "--tasks", "X1", "--n", "4", "--top-up", "--yes") == 0
    assert run_dirs_by_cell(h) == {("X1", 3): 1, ("X1", 4): 1, ("X1", 5): 1, ("X1", 6): 1}
