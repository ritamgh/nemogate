"""The runner: one run (`Runner.run_one`), many runs (`arun`/`run`), and the cross-owner `run()`.

The contract is in replay/README.md, section "runner.py". Live use is
`NEMOGATE_LIVE=1 uv run python -m replay.runner ...`, after `python -m replay.budget`.
"""

import argparse
import asyncio
import importlib
import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from common import env
from common.env import COMPACT_KEEP_ENV, COMPACT_TOKENS_ENV, is_live
from common.spend import run_scope, run_totals
from replay import record
from replay.app import App, AppTask, pairs
from replay.boundaries import (
    BoundaryRecorder,
    Crossing,
    ForkOrigin,
    Recording,
    copy_checkpoints,
    fork_point,
)
from replay.carriage import classify, compaction_evidence, write_carriage
from replay.fsdiff import diff, snapshot
from schemas.config import AppConfig, load_config
from schemas.contracts import EvaluateFn, MakeMiddlewareFn
from schemas.enums import Arm, Operator
from schemas.ledger import Ledger, load_ledger_yaml
from schemas.run import RunRecord
from schemas.trace import parse_trace_jsonl

ROOT = record.ROOT
LEDGER_PATH = ROOT / "nemogate.yaml"
CONFIG_PATH = ROOT / "config.yaml"
RUNS_DIR = ROOT / "runs"
CHECKPOINTS = "checkpoints.sqlite"  # file names in a run folder
BOUNDARIES = "boundaries.json"


def _default_client() -> Any:
    from contree_sdk import Contree  # imported here so tests never need the SDK client

    # contree-sdk resolves its credentials when `Contree()` is built and sends an unset
    # variable's name as the credential (FACTS §3, §9), so load `.env` and check first.
    env.load_dotenv()
    env.require_env("NEBIUS_API_KEY", "NEBIUS_PROJECT_ID")
    return Contree()


class Runner:
    def __init__(
        self,
        *,
        app: App,
        evaluate: EvaluateFn,
        make_middleware: MakeMiddlewareFn,
        ledger: Ledger,
        config: AppConfig,
        runs_dir: Path,
        client_factory: Callable[[], Any] = _default_client,
        backend_factory: Callable[[Any], Any] | None = None,
        ledger_hash_fn: Callable[[Ledger], str] = record.ledger_hash,
        recursion_limit: int = 60,
        allow_dirty: bool = False,
        repo_dir: Path = ROOT,
    ) -> None:
        self.app = app
        self.evaluate = evaluate
        self.make_middleware = make_middleware
        self.ledger = ledger
        self.config = config
        self.runs_dir = runs_dir
        self.client_factory = client_factory
        self.backend_factory = backend_factory or self._default_backend
        self.ledger_hash_fn = ledger_hash_fn
        self.recursion_limit = recursion_limit
        self.allow_dirty = allow_dirty
        self.repo_dir = repo_dir

    def _default_backend(self, session: Any) -> Any:
        # Imported here so tests never need the SDK.
        from replay.sandbox import ShieldedContreeSandbox

        return ShieldedContreeSandbox(session, cwd=self.app.repo_root)

    def _reproducibility(self, repo_sha: str) -> dict[str, Any]:
        """The five fields that make two records comparable (schemas.run.is_comparable)."""
        return {
            "model_ids": record.model_ids(self.config),
            "safe_config_hash": self.config.safe_config.hash,
            "ledger_hash": self.ledger_hash_fn(self.ledger),
            "repo_sha": repo_sha,
            "lockfile_hash": record.lockfile_hash(self.repo_dir / "uv.lock"),
        }

    async def run_one(
        self, task: AppTask, arm: Arm, operator: Operator, *, seed: int, repo_ref: str = "main"
    ) -> RunRecord:
        repo_sha = record.require_checkout_at(repo_ref, self.repo_dir, allow_dirty=self.allow_dirty)
        repro = self._reproducibility(repo_sha)
        return await self._run(task, arm, operator, seed=seed, repo_ref=repo_ref, repro=repro)

    async def fork_one(
        self,
        source_run_id: str,
        arm: Arm,
        operator: Operator,
        *,
        seed: int,
        repo_ref: str = "main",
    ) -> RunRecord:
        """A new run that continues the recorded run `source_run_id` from just before its first
        crossing of its target boundary, under `arm` and `operator`. Refused (ValueError) before
        any work unless the source ran the natural arm and the fork would be comparable."""
        repo_sha = record.require_checkout_at(repo_ref, self.repo_dir, allow_dirty=self.allow_dirty)
        source_dir = self.runs_dir / source_run_id
        source = RunRecord.model_validate_json(
            (source_dir / "record.json").read_text(encoding="utf-8")
        )
        if source.arm is not Arm.natural:
            raise ValueError(
                f"cannot fork {source_run_id}: it ran the {source.arm.value} arm, so the gate "
                "already acted before the boundary; fork from a natural run"
            )
        repro = self._reproducibility(repo_sha)
        differ = [name for name, value in repro.items() if getattr(source, name) != value]
        if differ:
            raise ValueError(
                f"cannot fork {source_run_id}: {', '.join(differ)} differ from the recorded run, "
                "so the fork would not be comparable with it"
            )
        # The compaction knob shapes the planner before and after the fork point alike, and only
        # carriage.json records it (it is not a reproducibility field), so it must match too.
        carriage = json.loads((source_dir / "carriage.json").read_text(encoding="utf-8"))
        if carriage.get("knobs") != _knobs():
            raise ValueError(
                f"cannot fork {source_run_id}: the compaction knobs {_knobs()} differ from the "
                f"recorded run's {carriage.get('knobs')}"
            )
        recording = Recording.model_validate_json(
            (source_dir / BOUNDARIES).read_text(encoding="utf-8")
        )
        crossing = fork_point(recording, source.boundary)
        if crossing is None:
            raise ValueError(
                f"cannot fork {source_run_id}: it never crossed {source.boundary}, so there is "
                "nothing to fork before"
            )
        task = {t.id: t for t in self.app.tasks()}[source.task_id]
        fork = _Fork(
            source_dir, recording, ForkOrigin(source_run_id=source_run_id, crossing=crossing)
        )
        return await self._run(
            task, arm, operator, seed=seed, repo_ref=repo_ref, repro=repro, fork=fork
        )

    async def _run(
        self,
        task: AppTask,
        arm: Arm,
        operator: Operator,
        *,
        seed: int,
        repo_ref: str,
        repro: dict[str, Any],
        fork: "_Fork | None" = None,
    ) -> RunRecord:
        run_id = record.new_run_id()
        run_dir = self.runs_dir / run_id

        # The gate loads the FULL ledger from this file (and pins run_config.target_constraint
        # itself), so the hash of the file is the hash the record carries. The oracle checks only
        # the target constraint, so it gets the scoped ledger.
        scoped = record.scoped_ledger(self.ledger, task.constraint)
        ledger_path = run_dir / "ledger.yaml"
        record.write_ledger(self.ledger, ledger_path)
        run_config = record.make_run_config(
            run_id=run_id,
            task=task,
            arm=arm,
            operator=operator,
            run_dir=run_dir,
            ledger_path=ledger_path,
            seed=seed,
            repo_ref=repo_ref,
        )
        record.write_json(run_dir / "run_config.json", run_config)
        if fork is not None:
            # The fork resumes in its own copy, so the recorded run is never written to.
            copy_checkpoints(fork.source_dir / CHECKPOINTS, run_dir / CHECKPOINTS)
            record.write_json(run_dir / "fork.json", fork.origin)

        client = self.client_factory()
        try:
            if fork is None:
                base = await self.app.base_image(client)
                start, thread_id = base, run_id
            else:
                base, thread_id = fork.recording.base_image, fork.recording.thread_id
                start = fork.origin.crossing.pre_image
            session = (await client.images.use(UUID(start))).session()
            checkpoint_id, crossings, compaction = await self._invoke(
                session, task, run_config, run_dir, thread_id, fork
            )
        except Exception:
            (run_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            raise
        record.write_json(
            run_dir / BOUNDARIES,
            Recording(base_image=base, thread_id=thread_id, crossings=crossings),
        )

        final = str(session.uuid)
        fs_diff = diff(
            await snapshot(client, base, self.app.repo_root),
            await snapshot(client, final, self.app.repo_root),
        )
        record.write_json(run_dir / "fs_diff.json", fs_diff)

        task_success, tail = await self.app.task_success(session, task)
        if not task_success:
            (run_dir / "task_check.txt").write_text(tail, encoding="utf-8")

        trace = self._read_trace(run_config, repro["safe_config_hash"], repro["ledger_hash"])

        violations = self.evaluate(scoped, trace, fs_diff)
        record.write_json(run_dir / "violations.json", violations)

        # Where the rule was lost, if it was. The compaction knob is recorded here because
        # nothing else hashes it (RunRecord and its reproducibility fields are frozen).
        carriage = classify(
            check=scoped.get(task.constraint).check,
            user_message=task.user_message,
            trace=trace,
            run_config=run_config,
            violations=violations,
            compaction=compaction,
        ).to_dict()
        carriage["knobs"] = _knobs()
        write_carriage(run_dir / "carriage.json", carriage)

        tokens, cost = run_totals(run_id)
        run_record = RunRecord(
            run_id=run_id,
            task_id=task.id,
            arm=arm,
            operator=operator,
            constraint_id=task.constraint,
            boundary=task.boundary,
            violations=tuple(dict.fromkeys(v.constraint_id for v in violations)),
            task_success=task_success,
            tokens=tokens,
            cost_usd=cost,
            checkpoint_id=checkpoint_id,
            contree_image=final,
            **repro,
        )
        record.write_json(run_dir / "record.json", run_record)
        return run_record

    @staticmethod
    def _read_trace(run_config: Any, safe_config_hash: str, ledger_hash: str) -> list[Any]:
        """The run's trace; RuntimeError unless the gate ran and hashed what the record carries."""
        trace_path = Path(run_config.trace_path)
        if not trace_path.is_file():
            raise RuntimeError(
                f"no trace file at {trace_path}: the gate did not run, and evaluating an empty "
                "trace would report zero violations"
            )
        trace = parse_trace_jsonl(trace_path.read_text(encoding="utf-8"))
        if not trace:
            raise RuntimeError(
                f"trace file {trace_path} has no events: the gate did not run, and evaluating an "
                "empty trace would report zero violations"
            )
        expected = {
            "run_id": run_config.run_id,
            "ledger_hash": ledger_hash,
            "safe_config_hash": safe_config_hash,
        }
        for event in trace:
            for field, want in expected.items():
                got = getattr(event, field)
                if got != want:
                    raise RuntimeError(
                        f"trace {trace_path}: {field} is {got!r} at step {event.step}, but the "
                        f"run record carries {want!r}"
                    )
        return trace

    async def _invoke(
        self,
        session: Any,
        task: AppTask,
        run_config: Any,
        run_dir: Path,
        thread_id: str,
        fork: "_Fork | None",
    ) -> tuple[str, tuple[Crossing, ...], dict[str, Any] | None]:
        """Build the graph on `session` and run the task, from the start or (a fork) from the
        fork point; return the final checkpoint id, the boundary crossings and the planner's
        compaction evidence (None if it never compacted)."""
        # Import path, constructor and close: langgraph/checkpoint/sqlite/aio.py:38 (the class),
        # :132-146 (`from_conn_string` is an async context manager that opens an aiosqlite
        # connection and closes it on exit); FACTS §5 ("SQLite checkpointer import path").
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self.recursion_limit,
        }
        recorder = BoundaryRecorder(session, run_config.target_boundary)
        # SpendHandler (common/llm.py:99) is a sync, non-inline callback handler. In async runs
        # langchain_core calls it through `run_in_executor(None, copy_context().run, ...)`
        # (langchain_core/callbacks/manager.py:404-421, :453-484), so it sees the ContextVar that
        # `run_scope` sets here. Nodes and sync tools get a copied context the same way
        # (langchain_core/runnables/config.py:627, :678-709). Building the gate and the graph is
        # inside the scope too, so a helper-model call made during setup is charged to the run.
        with run_scope(run_config.run_id):
            # The SAME list in both slots: the gate tells agents apart; the recorder acts only
            # on `task` calls, which only a delegating agent makes.
            middleware = [*self.make_middleware(run_config), recorder]
            async with AsyncSqliteSaver.from_conn_string(str(run_dir / CHECKPOINTS)) as saver:
                graph = self.app.build(
                    self.backend_factory(session),
                    middleware=middleware,
                    subagent_middleware=middleware,
                    checkpointer=saver,
                )
                if fork is None:
                    message = {"role": "user", "content": task.user_message}
                    await graph.ainvoke({"messages": [message]}, config=config)
                else:
                    crossing = fork.origin.crossing
                    start = {
                        **config,
                        "configurable": {
                            **config["configurable"],
                            "checkpoint_id": crossing.pre_checkpoint,
                        },
                    }
                    await _check_fork_point(graph, start, crossing.tool_call_id)
                    await graph.ainvoke(None, config=start)
                state = await graph.aget_state(config)
                children = await _children(graph, config) if recorder.crossings({}) else {}
                compaction = await compaction_evidence(
                    graph, config, state, run_config.target_boundary
                )
        checkpoint_id = state.config["configurable"]["checkpoint_id"]
        return checkpoint_id, recorder.crossings(children), compaction

    async def arun(
        self,
        task_ids: Sequence[str],
        arms: Sequence[Arm],
        operators: Sequence[Operator],
        n: int,
        repo_ref: str = "main",
    ) -> list[RunRecord]:
        """Every run, one after another: tasks in order, then (arm, operator) pairs, then `n`
        repetitions with seed = repetition index. Stops at the first exception."""
        by_id = {t.id: t for t in self.app.tasks()}
        tasks = [by_id[task_id] for task_id in task_ids]  # KeyError before any run starts
        records = []
        for task in tasks:
            for arm, operator in pairs(arms, operators):
                for seed in range(n):
                    records.append(
                        await self.run_one(task, arm, operator, seed=seed, repo_ref=repo_ref)
                    )
        return records

    def run(
        self,
        task_ids: Sequence[str],
        arms: Sequence[Arm],
        operators: Sequence[Operator],
        n: int,
        repo_ref: str = "main",
    ) -> list[RunRecord]:
        return asyncio.run(self.arun(task_ids, arms, operators, n, repo_ref))

    async def afork(
        self,
        source_run_id: str,
        arms: Sequence[Arm],
        operators: Sequence[Operator],
        n: int,
        repo_ref: str = "main",
    ) -> list[RunRecord]:
        """Every fork of one recorded run, one after another: (arm, operator) pairs, then `n`
        repetitions with seed = repetition index. Stops at the first exception."""
        records = []
        for arm, operator in pairs(arms, operators):
            for seed in range(n):
                records.append(
                    await self.fork_one(source_run_id, arm, operator, seed=seed, repo_ref=repo_ref)
                )
        return records

    def fork(
        self,
        source_run_id: str,
        arms: Sequence[Arm],
        operators: Sequence[Operator],
        n: int,
        repo_ref: str = "main",
    ) -> list[RunRecord]:
        return asyncio.run(self.afork(source_run_id, arms, operators, n, repo_ref))


def _knobs() -> dict[str, str | None]:
    """The compaction knobs as set now (null = unset); carriage.json records them."""
    return {name: os.environ.get(name) for name in (COMPACT_TOKENS_ENV, COMPACT_KEEP_ENV)}


@dataclass(frozen=True)
class _Fork:
    source_dir: Path
    recording: Recording  # the source run's boundaries.json
    origin: ForkOrigin  # what fork.json says: the source run and the crossing resumed before


async def _check_fork_point(graph: Any, start: dict, tool_call_id: str) -> None:
    """ValueError unless the checkpoint in `start` is the step that runs `tool_call_id` alone.

    Several tool calls of one model turn run in one step from one checkpoint (docs/FACTS.md
    section 5): resuming it runs all of them again, and the session image of one is not the
    image of the checkpoint, so such a crossing cannot be forked.
    """
    snap = await graph.aget_state(start)
    messages = snap.values.get("messages") or []
    calls = (getattr(messages[-1], "tool_calls", None) or []) if messages else []
    ids = [call["id"] for call in calls]
    if ids == [tool_call_id] and tuple(snap.next) == ("tools",):
        return
    checkpoint = start["configurable"]["checkpoint_id"]
    if tool_call_id in ids:
        raise ValueError(
            f"cannot fork before tool call {tool_call_id}: checkpoint {checkpoint} runs "
            f"{len(ids)} tool calls ({', '.join(ids)}) in one step, and resuming it runs them all"
        )
    raise ValueError(
        f"checkpoint {checkpoint} is not the step that runs tool call {tool_call_id} "
        f"(its pending tool calls: {', '.join(ids) or 'none'}; next: {snap.next})"
    )


async def _children(graph: Any, config: dict) -> dict[str, str]:
    """Root checkpoint id -> id of its newest child on the thread: the run's own next checkpoint
    (a fork's copy also holds its source's, which are older)."""
    children: dict[str, str] = {}
    async for snap in graph.aget_state_history(config):  # newest first
        if snap.parent_config:
            parent = snap.parent_config["configurable"]["checkpoint_id"]
            children.setdefault(parent, snap.config["configurable"]["checkpoint_id"])
    return children


def _import(package: str) -> Any:
    try:
        return importlib.import_module(package)
    except ModuleNotFoundError as exc:
        if exc.name != package:
            raise
        raise RuntimeError(f"cannot run: the `{package}` package is not available ({exc})") from exc


def require_ignored_runs_dir(runs_dir: Path, repo_dir: Path = ROOT) -> None:
    """ValueError unless `runs_dir` is outside the checkout or git ignores what a run writes there.

    Run folders inside the checkout that git sees as untracked make the checkout dirty, so the
    second run would fail `require_checkout_at` after the first had spent credit.
    """
    path, root = runs_dir.resolve(), repo_dir.resolve()
    if not path.is_relative_to(root):
        return
    probe = (path / "run-folder-probe").relative_to(root)
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", str(probe)], cwd=root, capture_output=True, check=False
    )
    if ignored.returncode != 0:
        raise ValueError(
            f"--runs-dir {runs_dir} is inside the checkout and not git-ignored: its run folders "
            "would make the checkout dirty and fail the next run. Use a directory under runs/ "
            "or outside the checkout."
        )


def default_runner(runs_dir: Path | None = None) -> Runner:
    """The Runner `run()` uses (README, "Defaults"); `runs_dir` None means `RUNS_DIR`. Also the
    way to fork with the defaults."""
    evaluate = _import("oracle").evaluate
    make_middleware = _import("gate").make_middleware
    if not LEDGER_PATH.is_file():
        raise RuntimeError(f"cannot run: the ledger file {LEDGER_PATH} does not exist")
    ledger = load_ledger_yaml(LEDGER_PATH.read_text(encoding="utf-8"))
    from refapp.app import APP

    return Runner(
        app=APP,
        evaluate=evaluate,
        make_middleware=make_middleware,
        ledger=ledger,
        config=load_config(CONFIG_PATH),
        runs_dir=RUNS_DIR if runs_dir is None else runs_dir,
    )


def run(
    task_ids: list[str],
    arms: list[Arm],
    operators: list[Operator],
    n: int,
    repo_ref: str = "main",
) -> list[RunRecord]:
    """`schemas.contracts.RunFn`: the default Runner (README "Defaults") over these runs."""
    return default_runner().run(task_ids, arms, operators, n, repo_ref)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m replay.runner", description=__doc__)
    parser.add_argument("--tasks", required=True, nargs="+", help="task ids")
    parser.add_argument("--arms", required=True, nargs="+", type=Arm, choices=list(Arm))
    parser.add_argument(
        "--operators", required=True, nargs="+", type=Operator, choices=list(Operator)
    )
    parser.add_argument("--n", required=True, type=int, help="repetitions per task and pair")
    parser.add_argument("--repo-ref", default="main")
    parser.add_argument(
        "--runs-dir",
        type=Path,
        help="write run folders here instead of runs/ (keep compaction runs apart)",
    )
    parser.add_argument("--yes", action="store_true", help="confirm the run count and go")
    args = parser.parse_args(argv)

    if args.runs_dir is not None:
        try:
            require_ignored_runs_dir(args.runs_dir)
        except ValueError as exc:
            print(f"refusing to run: {exc}", file=sys.stderr)
            return 2
    if not is_live():
        print(
            "refusing to run: set NEMOGATE_LIVE=1 (real model calls spend credit)", file=sys.stderr
        )
        return 2
    planned = len(args.tasks) * len(pairs(args.arms, args.operators)) * args.n
    print(f"planned runs: {planned}")
    if not args.yes:
        print(
            "not started: run `python -m replay.budget` to check this count against the budget "
            "(AGENTS.md rule 3), then repeat with --yes",
            file=sys.stderr,
        )
        return 1
    if args.runs_dir is None:
        records = run(args.tasks, args.arms, args.operators, args.n, args.repo_ref)
    else:
        records = default_runner(args.runs_dir).run(
            args.tasks, args.arms, args.operators, args.n, args.repo_ref
        )
    for r in records:
        print(
            f"{r.run_id} task={r.task_id} arm={r.arm.value} operator={r.operator.value} "
            f"violations={','.join(r.violations) or '-'} task_success={r.task_success} "
            f"cost_usd={r.cost_usd:.4f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
