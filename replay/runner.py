"""The runner: one run (`Runner.run_one`), many runs (`arun`/`run`), and the cross-owner `run()`.

The contract is in replay/README.md, section "runner.py". Live use is
`NEMOGATE_LIVE=1 uv run python -m replay.runner ...`, after `python -m replay.budget`.
"""

import argparse
import asyncio
import importlib
import os
import subprocess
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from common import env
from common.env import COMPACT_KEEP_ENV, COMPACT_TOKENS_ENV, is_live
from common.spend import run_scope, run_totals
from replay import record
from replay.app import App, AppTask, pairs
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

    async def run_one(
        self, task: AppTask, arm: Arm, operator: Operator, *, seed: int, repo_ref: str = "main"
    ) -> RunRecord:
        repo_sha = record.require_checkout_at(repo_ref, self.repo_dir, allow_dirty=self.allow_dirty)
        run_id = record.new_run_id()
        run_dir = self.runs_dir / run_id

        # The gate loads the FULL ledger from this file (and pins run_config.target_constraint
        # itself), so the hash of the file is the hash the record carries. The oracle checks only
        # the target constraint, so it gets the scoped ledger.
        scoped = record.scoped_ledger(self.ledger, task.constraint)
        ledger_path = run_dir / "ledger.yaml"
        record.write_ledger(self.ledger, ledger_path)
        ledger_hash = self.ledger_hash_fn(self.ledger)
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

        client = self.client_factory()
        try:
            base = await self.app.base_image(client)
            session = (await client.images.use(UUID(base))).session()
            checkpoint_id, compaction = await self._invoke(
                client, session, task, run_config, run_dir
            )
        except Exception:
            (run_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            raise

        final = str(session.uuid)
        fs_diff = diff(
            await snapshot(client, base, self.app.repo_root),
            await snapshot(client, final, self.app.repo_root),
        )
        record.write_json(run_dir / "fs_diff.json", fs_diff)

        task_success, tail = await self.app.task_success(session, task)
        if not task_success:
            (run_dir / "task_check.txt").write_text(tail, encoding="utf-8")

        safe_config_hash = self.config.safe_config.hash
        trace = self._read_trace(run_config, safe_config_hash, ledger_hash)

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
        carriage["knobs"] = {
            name: os.environ.get(name) for name in (COMPACT_TOKENS_ENV, COMPACT_KEEP_ENV)
        }
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
            model_ids=record.model_ids(self.config),
            safe_config_hash=safe_config_hash,
            ledger_hash=ledger_hash,
            repo_sha=repo_sha,
            lockfile_hash=record.lockfile_hash(self.repo_dir / "uv.lock"),
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
        self, client: Any, session: Any, task: AppTask, run_config: Any, run_dir: Path
    ) -> tuple[str, dict[str, Any] | None]:
        """Build the graph on a fresh session, run the task and return the final checkpoint id
        and the planner's compaction evidence (None if it never compacted)."""
        # Import path, constructor and close: langgraph/checkpoint/sqlite/aio.py:38 (the class),
        # :132-146 (`from_conn_string` is an async context manager that opens an aiosqlite
        # connection and closes it on exit); FACTS §5 ("SQLite checkpointer import path").
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        config = {
            "configurable": {"thread_id": run_config.run_id},
            "recursion_limit": self.recursion_limit,
        }
        # SpendHandler (common/llm.py:99) is a sync, non-inline callback handler. In async runs
        # langchain_core calls it through `run_in_executor(None, copy_context().run, ...)`
        # (langchain_core/callbacks/manager.py:404-421, :453-484), so it sees the ContextVar that
        # `run_scope` sets here. Nodes and sync tools get a copied context the same way
        # (langchain_core/runnables/config.py:627, :678-709). Building the gate and the graph is
        # inside the scope too, so a helper-model call made during setup is charged to the run.
        with run_scope(run_config.run_id):
            middleware = self.make_middleware(run_config)
            async with AsyncSqliteSaver.from_conn_string(
                str(run_dir / "checkpoints.sqlite")
            ) as saver:
                graph = self.app.build(
                    self.backend_factory(session),
                    middleware=middleware,
                    subagent_middleware=middleware,  # the SAME list: the gate tells agents apart
                    checkpointer=saver,
                )
                await graph.ainvoke(
                    {"messages": [{"role": "user", "content": task.user_message}]}, config=config
                )
                state = await graph.aget_state(config)
                compaction = await compaction_evidence(
                    graph, config, state, run_config.target_boundary
                )
        return state.config["configurable"]["checkpoint_id"], compaction

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
    """The default Runner (README, "Defaults"); `runs_dir` None means `RUNS_DIR`."""
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
