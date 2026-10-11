"""The runner: one run (`Runner.run_one`), many runs (`arun`/`run`), and the cross-owner `run()`.

The contract is in replay/README.md, section "runner.py". Live use is
`NEMOGATE_LIVE=1 uv run python -m replay.runner ...`, after `python -m replay.budget`.
"""

import argparse
import asyncio
import importlib
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from common.env import is_live
from common.spend import run_scope, run_totals
from replay import record
from replay.app import App, AppTask, pairs
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

    return Contree()


def _default_backend(session: Any) -> Any:
    from contree_sdk.langchain.sandbox import ContreeSandbox

    return ContreeSandbox(session)


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
        backend_factory: Callable[[Any], Any] = _default_backend,
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
        self.backend_factory = backend_factory
        self.ledger_hash_fn = ledger_hash_fn
        self.recursion_limit = recursion_limit
        self.allow_dirty = allow_dirty
        self.repo_dir = repo_dir

    async def run_one(
        self, task: AppTask, arm: Arm, operator: Operator, *, seed: int, repo_ref: str = "main"
    ) -> RunRecord:
        repo_sha = record.require_checkout_at(repo_ref, self.repo_dir, allow_dirty=self.allow_dirty)
        run_id = record.new_run_id()
        run_dir = self.runs_dir / run_id

        # The gate pins and the oracle checks only the target constraint, but the hash covers the
        # whole ledger so runs on different target constraints stay comparable.
        scoped = record.scoped_ledger(self.ledger, task.constraint)
        ledger_path = run_dir / "ledger.yaml"
        record.write_ledger(scoped, ledger_path)
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
            checkpoint_id = await self._invoke(client, session, task, run_config, run_dir)
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

        trace_path = Path(run_config.trace_path)
        if not trace_path.is_file():
            raise RuntimeError(
                f"no trace file at {trace_path}: the gate did not run, and evaluating an empty "
                "trace would report zero violations"
            )
        trace = parse_trace_jsonl(trace_path.read_text(encoding="utf-8"))

        violations = self.evaluate(scoped, trace, fs_diff)
        record.write_json(run_dir / "violations.json", violations)

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
            safe_config_hash=self.config.safe_config.hash,
            ledger_hash=ledger_hash,
            repo_sha=repo_sha,
            lockfile_hash=record.lockfile_hash(self.repo_dir / "uv.lock"),
        )
        record.write_json(run_dir / "record.json", run_record)
        return run_record

    async def _invoke(
        self, client: Any, session: Any, task: AppTask, run_config: Any, run_dir: Path
    ) -> str:
        """Build the graph on a fresh session, run the task and return the final checkpoint id."""
        # Import path, constructor and close: langgraph/checkpoint/sqlite/aio.py:38 (the class),
        # :132-146 (`from_conn_string` is an async context manager that opens an aiosqlite
        # connection and closes it on exit); FACTS §5 ("SQLite checkpointer import path").
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        middleware = self.make_middleware(run_config)
        config = {
            "configurable": {"thread_id": run_config.run_id},
            "recursion_limit": self.recursion_limit,
        }
        async with AsyncSqliteSaver.from_conn_string(str(run_dir / "checkpoints.sqlite")) as saver:
            graph = self.app.build(
                self.backend_factory(session),
                middleware=middleware,
                subagent_middleware=middleware,  # the SAME list: the gate tells the agents apart
                checkpointer=saver,
            )
            # SpendHandler (common/llm.py:99) is a sync, non-inline callback handler. In async
            # runs langchain_core calls it through `run_in_executor(None, copy_context().run,
            # ...)` (langchain_core/callbacks/manager.py:404-421, :453-484), so it sees the
            # ContextVar that `run_scope` sets here. Nodes and sync tools get a copied context the
            # same way (langchain_core/runnables/config.py:627, :678-709).
            with run_scope(run_config.run_id):
                await graph.ainvoke(
                    {"messages": [{"role": "user", "content": task.user_message}]}, config=config
                )
                state = await graph.aget_state(config)
        return state.config["configurable"]["checkpoint_id"]

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


def run(
    task_ids: list[str],
    arms: list[Arm],
    operators: list[Operator],
    n: int,
    repo_ref: str = "main",
) -> list[RunRecord]:
    """`schemas.contracts.RunFn`: the default Runner (README, "Defaults") over these runs."""
    evaluate = _import("oracle").evaluate
    make_middleware = _import("gate").make_middleware
    if not LEDGER_PATH.is_file():
        raise RuntimeError(f"cannot run: the ledger file {LEDGER_PATH} does not exist")
    ledger = load_ledger_yaml(LEDGER_PATH.read_text(encoding="utf-8"))
    from refapp.app import APP

    runner = Runner(
        app=APP,
        evaluate=evaluate,
        make_middleware=make_middleware,
        ledger=ledger,
        config=load_config(CONFIG_PATH),
        runs_dir=RUNS_DIR,
    )
    return runner.run(task_ids, arms, operators, n, repo_ref)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m replay.runner", description=__doc__)
    parser.add_argument("--tasks", required=True, nargs="+", help="task ids")
    parser.add_argument("--arms", required=True, nargs="+", type=Arm, choices=list(Arm))
    parser.add_argument(
        "--operators", required=True, nargs="+", type=Operator, choices=list(Operator)
    )
    parser.add_argument("--n", required=True, type=int, help="repetitions per task and pair")
    parser.add_argument("--repo-ref", default="main")
    parser.add_argument("--yes", action="store_true", help="confirm the run count and go")
    args = parser.parse_args(argv)

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
    for r in run(args.tasks, args.arms, args.operators, args.n, args.repo_ref):
        print(
            f"{r.run_id} task={r.task_id} arm={r.arm.value} operator={r.operator.value} "
            f"violations={','.join(r.violations) or '-'} task_success={r.task_success} "
            f"cost_usd={r.cost_usd:.4f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
