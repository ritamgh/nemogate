"""replay/runner.py with fakes for Contree, the app and the gate/oracle: no sandbox, no model."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage

from common import env
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
    def __init__(self, session, run_config, checkpointer, behaviour):
        self.session, self.run_config = session, run_config
        self.checkpointer, self.behaviour = checkpointer, behaviour
        self.invoked: list[tuple] = []
        self.state_configs: list[dict] = []

    async def ainvoke(self, payload, config):
        self.invoked.append((payload, config))
        await self.checkpointer.setup()  # creates the real sqlite file
        if self.behaviour.get("raises"):
            raise ValueError(self.behaviour["raises"])
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
            content="", tool_calls=[{"name": "task", "id": "c1", "args": {}, "type": "tool_call"}]
        )
        yield SimpleNamespace(
            metadata={"step": 3}, values={"_summarization_event": event, "messages": [delegation]}
        )
        yield SimpleNamespace(metadata={"step": 0}, values={"messages": []})


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

    def tasks(self):
        return [FakeTask("X1", "C-001"), FakeTask("X2", "C-002")]

    async def base_image(self, client) -> str:
        return BASE

    def build(self, backend, *, middleware, subagent_middleware, checkpointer):
        self.built = {"middleware": middleware, "subagent_middleware": subagent_middleware}
        self.graph = FakeGraph(
            backend.session, middleware[0].run_config, checkpointer, self.behaviour
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
    h = Harness(tmp_path)
    asyncio.run(h.run_one())
    (mw_list,) = h.middleware_lists
    assert h.app.built["middleware"] is mw_list
    assert h.app.built["subagent_middleware"] is mw_list


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

    def fake_run(task_ids, arms, operators, n, repo_ref):
        got.append((task_ids, arms, operators, n, repo_ref))
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

    monkeypatch.setattr(runner, "run", fake_run)
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
        "label": "DROPPED",
        "reason": "carried=False violated=True",
        "anchors": ["rm"],
        "in_planner": True,
        "carried": False,
        "violated": True,
        "natural_guard": True,
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
    }
    assert carriage["knobs"] == {"NEMOGATE_COMPACT_TOKENS": "8000", "NEMOGATE_COMPACT_KEEP": "2"}


def test_carriage_json_is_unknown_when_nothing_was_delegated(tmp_path, monkeypatch):
    # Breaks if a run with no handoff at the target boundary gets a carried or dropped label.
    carriage = run_natural(tmp_path, {}, monkeypatch)
    assert carriage["label"] == "UNKNOWN"
    assert carriage["knobs"] == {"NEMOGATE_COMPACT_TOKENS": None, "NEMOGATE_COMPACT_KEEP": None}
