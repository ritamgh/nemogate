# replay: the runner, the filesystem diff and the budget check

Owner: A. Pipeline code: the generality rule (AGENTS.md rule 5) applies to everything here
outside `tests/`. Nothing in replay/ names a task, a constraint, the seed repo or `refapp`
except the one default noted under "Defaults".

This file is the contract between the modules below. Change it only together with the code.

Week 1 scope (docs/SPEC.md, "Minimal runner for the Oct 12 kill gate"): one run = write the run
config, launch the app with the gate's middleware, call `evaluate()`, write the run record with
its reproducibility fields. Week 2 (C4 record and fork): every run records the checkpoint and
the sandbox image at each boundary it crosses (`boundaries.json`), and a fork continues a
recorded natural run from just before its target boundary under another arm, so each arm x N
replays only the part after the boundary. Statistics are a separate card.

## Layout

```
replay/
  app.py        App and AppTask Protocols, and pairs(arms, operators) (committed with this file)
  fsdiff.py     snapshot an image's repo, diff two snapshots
  record.py     run ids, run config, scoped ledger, reproducibility fields, run folder files
  boundaries.py BoundaryRecorder middleware, boundaries.json / fork.json formats, fork point,
                checkpoint database copy
  runner.py     Runner (one run, many runs, forks) and run(), the cross-owner function
  sandbox.py    ShieldedContreeSandbox(session, cwd=None): ContreeSandbox whose `aexecute` survives
                its caller being cancelled (deepagents' grep/glob `wait_for`), so the session is
                never left executing, and which runs every command from `cwd`
  budget.py     N per arm from a bucket and a cost per run; `python -m replay.budget`
refapp/app.py   the reference app's App (owned by refapp, listed here because the runner uses it)
```

## fsdiff.py

```python
SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache"})   # generic Python caches only
SKIP_SUFFIXES = frozenset({".pyc"})
async def snapshot(client, image_uuid: str, root: str, *, timeout: int = 120) -> dict[str, str]
    # repo-relative POSIX path -> "sha256:<hex>" for every regular file under `root` in the image,
    # "symlink:<target>" for a symlink (never followed). SKIP_DIRS names skip real DIRECTORIES
    # only (a regular file or symlink called `__pycache__` is recorded); SKIP_SUFFIXES skip
    # REGULAR FILES only (a directory `pkg.pyc/` is walked, a symlink `x.pyc` is recorded).
    # Runs ONE command, disposable, on a plain image: `image = await client.images.use(UUID(uuid))`
    # then `await image.run(shell=..., disposable=True, timeout=timeout)` (never on a session:
    # FACTS §3). The command is `python3 -c <script>` that prints one JSON object on stdout
    # (handles any file name: spaces, newlines, unicode). Raises RuntimeError on a non-zero
    # exit, truncated output (`result.result.truncated` or equivalent) or unparsable JSON.
def diff(base: dict[str, str], final: dict[str, str]) -> list[FsChange]
    # added (only in final), deleted (only in base), modified (both, different value).
    # Sorted by path. Unchanged paths are absent.
```

## record.py

```python
ROOT = Path(__file__).resolve().parents[1]   # the checkout this code runs from
def new_run_id() -> str                      # "r-<UTC %Y%m%dT%H%M%S>-<6 lowercase hex>"
def git_sha(ref: str, repo_dir: Path = ROOT) -> str
    # full 40-hex commit sha of `git rev-parse --verify <ref>^{commit}`; ValueError if unknown
def require_checkout_at(ref: str, repo_dir: Path = ROOT, *, allow_dirty: bool = False) -> str
    # The code that runs is this checkout, so a run may only claim repo_sha = HEAD.
    # ValueError if git_sha(ref) != git_sha("HEAD") (re-running another ref is week 2), or if
    # `git status --porcelain` is non-empty and not allow_dirty: modified tracked files and
    # untracked files that .gitignore does not cover both count; ignored files never do.
    # Returns the sha.
def lockfile_hash(path: Path = ROOT / "uv.lock") -> str      # first 12 hex of sha256(bytes)
def model_ids(config: AppConfig) -> dict[Role, str]           # every role's configured id
def scoped_ledger(ledger: Ledger, constraint_id: str) -> Ledger   # only that entry; KeyError
def ledger_hash(ledger: Ledger) -> str
    # first 12 hex of sha256 over compact sorted-key JSON of ledger.model_dump(mode="json").
    # The default until B's loader returns its own (SPEC: "returned by B's loader").
def write_ledger(ledger: Ledger, path: Path) -> None   # YAML that schemas.ledger.load_ledger_yaml
                                                       # reads back to an equal Ledger
def make_run_config(*, run_id, task: AppTask, arm, operator, run_dir: Path, ledger_path: Path,
                    seed: int, repo_ref: str) -> RunConfig
    # target_boundary/target_constraint from the task; trace_path = run_dir/"trace.jsonl";
    # paths are stored as given (str).
def write_json(path: Path, model: BaseModel | list[BaseModel]) -> None
    # pretty JSON (indent 2, trailing newline) of model_dump(mode="json"), parents created
```

## boundaries.py

```python
class Crossing(BaseModel):   # STRICT; one `task` call: its delegation and its return
    tool_call_id: str
    delegation: BoundaryId   # delegation:<sender>-><subagent>
    returned: BoundaryId     # return:<subagent>-><sender>
    pre_checkpoint: str      # root checkpoint of the step that runs the call: the fork point
    pre_image: str           # session.uuid when the delegation crossed (before the call ran)
    post_checkpoint: str | None  # this run's root checkpoint right after that step
    post_image: str | None       # session.uuid when the reply crossed; None if it never did
class Recording(BaseModel):  # STRICT; run_dir/boundaries.json
    base_image: str          # the image the app's run started from (a fork keeps its source's)
    thread_id: str           # the checkpoint thread (a fork keeps its source's)
    crossings: tuple[Crossing, ...]   # in delegation order
class ForkOrigin(BaseModel): # STRICT; run_dir/fork.json of a fork
    source_run_id: str
    crossing: Crossing       # the source crossing the fork resumed before

class BoundaryRecorder(AgentMiddleware):
    def __init__(self, session, target_boundary: str)
    # wrap_tool_call and awrap_tool_call. Acts only on `task` calls whose subagent_type is an
    # agent name ([A-Za-z0-9_-]+), as the gate does; every other call passes straight through.
    # Before the call: pre_checkpoint = runtime.config["configurable"]["checkpoint_map"][""]
    # (checkpoint_id is None inside a tool call, docs/FACTS.md §5) and pre_image = session.uuid.
    # After it returns: post_image = session.uuid. Sender = metadata lc_agent_name, else the
    # sender of target_boundary (X in delegation:X->Y and return:Y->X), as the gate names it.
    def crossings(self, children: Mapping[str, str]) -> tuple[Crossing, ...]
    # children: root checkpoint id -> id of this run's next checkpoint (post_checkpoint).
def fork_point(recording, boundary) -> Crossing | None
    # The FIRST crossing whose delegation or return is `boundary` (so every crossing of the
    # boundary lies after the fork). A return forks before its delegation: no root checkpoint
    # lies between the subagent finishing and its reply, so the call runs again.
def copy_checkpoints(src: Path, dst: Path) -> None
    # sqlite3 backup API (rows still in the -wal file included); FileNotFoundError if no src.
```

## runner.py

```python
class Runner:
    def __init__(self, *, app: App, evaluate: EvaluateFn, make_middleware: MakeMiddlewareFn,
                 ledger: Ledger, config: AppConfig, runs_dir: Path,
                 client_factory=<Contree>,
                 backend_factory=None,  # None: ShieldedContreeSandbox(session, cwd=app.repo_root)
                 ledger_hash_fn=record.ledger_hash, recursion_limit: int = 60,
                 allow_dirty: bool = False, repo_dir: Path = ROOT)
    async def run_one(self, task: AppTask, arm: Arm, operator: Operator, *, seed: int,
                      repo_ref: str = "main") -> RunRecord
    async def fork_one(self, source_run_id: str, arm: Arm, operator: Operator, *, seed: int,
                       repo_ref: str = "main") -> RunRecord
    async def arun(self, task_ids, arms, operators, n, repo_ref="main") -> list[RunRecord]
    def run(self, task_ids, arms, operators, n, repo_ref="main") -> list[RunRecord]  # asyncio.run
    async def afork(self, source_run_id, arms, operators, n, repo_ref="main") -> list[RunRecord]
    def fork(self, source_run_id, arms, operators, n, repo_ref="main") -> list[RunRecord]

def default_runner() -> Runner    # the Runner run() uses (see Defaults)
def run(task_ids, arms, operators, n, repo_ref="main") -> list[RunRecord]
    # schemas.contracts.RunFn. default_runner().run(...). Full runs only; it does not fork.
```

`run_one`, in order (each step's output goes into `runs_dir/<run_id>/`):

1. `repo_sha = record.require_checkout_at(repo_ref, ...)`; the five reproducibility fields
   (`model_ids`, `safe_config_hash`, `ledger_hash = ledger_hash_fn(self.ledger)` called once,
   `repo_sha`, `lockfile_hash`) are computed here, before any work; `run_id = new_run_id()`;
   `run_dir = runs_dir / run_id`.
2. The FULL ledger (`record.write_ledger(self.ledger, ...)`) is written to `run_dir/ledger.yaml`
   and `RunConfig.ledger_path` points at it: the gate loads that file and pins
   `run_config.target_constraint` itself, so the hash of the file the gate loads is the hash the
   record carries. The oracle gets `scoped_ledger(self.ledger, task.constraint)` (step 9). Run
   config written to `run_dir/run_config.json`.
3. `client = client_factory()`; `base = await app.base_image(client)`;
   `session = (await client.images.use(UUID(base))).session()`; `backend = backend_factory(session)`.
4. `mw = [*make_middleware(run_config), BoundaryRecorder(session, run_config.target_boundary)]`;
   graph = `app.build(backend, middleware=mw, subagent_middleware=mw, checkpointer=saver)` with
   `saver` an `AsyncSqliteSaver` on `run_dir/checkpoints.sqlite` (closed after the run). The SAME
   list goes to both slots: the gate tells planner from subagents by `lc_agent_name`
   (refapp/README.md, "Paths and agent names"); the recorder acts only on `task` calls.
   `make_middleware(run_config)`, the graph build and the invoke all happen inside
   `common.spend.run_scope(run_id)`, so a helper-model call made while setting up the gate is
   charged to the run.
5. `await graph.ainvoke({"messages": [{"role": "user", "content": task.user_message}]},
   config={"configurable": {"thread_id": run_id}, "recursion_limit": recursion_limit})`.
   `checkpoint_id = (await graph.aget_state(same config)).config["configurable"]["checkpoint_id"]`.
   If the recorder saw a crossing, `aget_state_history(same config)` gives each root
   checkpoint's newest child (the post checkpoints). `Recording(base_image=base,
   thread_id=run_id, crossings)` -> `run_dir/boundaries.json`.
6. `final = str(session.uuid)`; `fs_diff = diff(await snapshot(client, base, app.repo_root),
   await snapshot(client, final, app.repo_root))` -> `run_dir/fs_diff.json`.
7. `task_success, tail = await app.task_success(session, task)`; a failing tail goes to
   `run_dir/task_check.txt`.
8. `trace = parse_trace_jsonl(Path(run_config.trace_path).read_text())`. A missing trace file
   or one with no events is an error (RuntimeError naming the path): no trace means the gate
   did not run, and evaluating an empty trace would report zero violations. Every event's
   `run_id` must equal the run's id, and its `ledger_hash` and `safe_config_hash` must equal
   the values the record will carry; otherwise RuntimeError naming the field, both values and
   the event's step (no `record.json`). A gate that hashes a different ledger or config fails
   loudly.
9. `violations = evaluate(scoped, trace, fs_diff)`; `RunRecord.violations` = the unique
   constraint ids in first-seen order. Full list -> `run_dir/violations.json`.
10. `tokens, cost = common.spend.run_totals(run_id)`; every Role present (0 if unused).
11. `RunRecord(... contree_image=final, checkpoint_id=..., <the five fields from step 1>)` ->
    `run_dir/record.json`; return it.

If steps 3-5 raise (base image, session, build or invoke), write the traceback to `run_dir/error.txt` and re-raise: a
crashed run has no record (and no `boundaries.json`). `arun` runs sequentially (no concurrency
in week 1), tasks in the order of `task_ids` (KeyError on an unknown id), then pairs, then `n`
repetitions with `seed = <repetition index>`; it stops at the first exception.

`fork_one(source_run_id, arm, operator, *, seed, repo_ref)`: a new run (new run id and folder)
that continues the recorded run `runs_dir/<source_run_id>/` from just before its first crossing
of its target boundary. It reads only that folder, so it works from a new process.

1. Refused with ValueError before any client, folder or graph exists if: the checkout is not
   `repo_ref` (as in run_one); the source's `record.json` says another arm than `natural` (the
   gate acts in every other arm, so the part before the boundary would differ from what the
   app does on its own); any of the five reproducibility fields computed now differs from the
   source record's (the fork would not be `is_comparable` with it; the message names the
   fields); `fork_point(<source boundaries.json>, source.boundary)` is None. A missing
   `record.json` or `boundaries.json` raises FileNotFoundError naming it. The task is
   `app.tasks()[source.task_id]` (KeyError if gone).
2. As run_one steps 2-11, except: `copy_checkpoints(source/checkpoints.sqlite,
   run_dir/checkpoints.sqlite)` (the fork writes into its own copy; the source folder is never
   written) and `run_dir/fork.json` (ForkOrigin) are written after the run config; the session
   starts from `crossing.pre_image`; the invoke is `await graph.ainvoke(None, config)` with
   config `{"configurable": {"thread_id": <source thread>, "checkpoint_id":
   crossing.pre_checkpoint}, "recursion_limit": ...}`, preceded by a check that this checkpoint
   is the step that runs `crossing.tool_call_id` and nothing else (`next == ("tools",)`, the
   last message's tool call ids == [that id]). Several tool calls in one model turn run in one
   step from one checkpoint and resuming re-runs them all, so that is refused with a
   ValueError naming the count and ids (in `error.txt`, no record). The final checkpoint is
   `aget_state` of the source thread (latest = the fork's). `boundaries.json` keeps the
   source's `base_image` and `thread_id`; resuming saves a `source: "fork"` checkpoint (a copy
   of the fork point) and the step runs from it, so the fork's own crossing has that as its
   `pre_checkpoint` (docs/FACTS.md §5).
3. Scope: the trace holds only what happens after the fork point (the gate runs only there);
   `fs_diff` is from the source's `base_image` to the fork's final image, the run's whole
   effect, as for a full run. The fork's gate starts fresh: approvals given before the fork
   point are not known to it.

`afork(source_run_id, arms, operators, n, repo_ref)` mirrors `arun` for one source run: pairs,
then `n` repetitions with `seed = <repetition index>`, sequential, stops at the first exception.
`fork` is its `asyncio.run` wrapper. Use `default_runner().fork(...)` for forks with the
defaults (live, after `python -m replay.budget`).

## budget.py

```python
SHARES = {"dev": 0.20, "kill_gate": 0.10, "main_sweep": 0.40, "repair": 0.15, "final": 0.15}
    # docs/SPEC.md "Budget"; amount = share * config.budget.total_usd (per person)
def runs_per_cell(bucket_usd: float, cost_per_run: float, n_pairs: int, n_tasks: int) -> int
    # floor(bucket / (cost_per_run * n_pairs * n_tasks)); ValueError unless all inputs > 0
def plan(*, bucket: str, cost_per_run: float, arms, operators, n_tasks: int, config: AppConfig,
         spent_usd: float) -> Plan
    # Plan(bucket, bucket_usd, cost_per_run, pairs, n_tasks, n, planned_usd, spent_usd,
    #      stop_usd, capped): n from runs_per_cell with n_pairs = len(app.pairs(...));
    # stop_usd = stop_at_fraction * total_usd; if spent + planned would pass stop_usd, n is
    # lowered to the largest value that fits and capped=True.
def mean_cost(records: Iterable[RunRecord]) -> float     # ValueError on none
main(): `python -m replay.budget --bucket kill_gate --arms natural mutated --operators none drop
        --tasks <id> <id> (--cost-per-run X | --from-runs runs/)` prints the plan, one fact per
        line, ending with `N per arm per task: <n>`. Exit 1 when n < 1. spent_usd from
        common.spend.total_spent(). No model calls, no Contree.
```

## Accepted deviations from the first spec

- `run()` also loads the config from `ROOT/config.yaml` (the Defaults list names no config
  source).
- `LEDGER_PATH`, `CONFIG_PATH` and `RUNS_DIR` are module constants in `runner.py` (tests
  monkeypatch `LEDGER_PATH`).
- `error.txt` covers steps 3-5 (base image, session, build, invoke), not only the invoke.
- `arun` resolves every task id before the first run, so an unknown id raises `KeyError`
  without wasting a run.
- `python -m replay.runner --tasks .. --arms .. --operators .. --n N [--repo-ref REF] [--yes]`
  refuses with exit code 2 unless `NEMOGATE_LIVE=1`; it prints `planned runs: N` and, without
  `--yes`, exits 1 pointing at `python -m replay.budget`. With `--yes` it runs and prints one
  line per record (`<run_id> task=.. arm=.. operator=.. violations=C-1,C-2|- task_success=..
  cost_usd=0.0000`).
- The default client loads `.env` (`common.env.load_dotenv`) and checks `NEBIUS_API_KEY` and
  `NEBIUS_PROJECT_ID` (`require_env`) before building `Contree()`.

## Defaults

`replay.runner.default_runner()` (used by `run()` and the CLI) builds: `app = refapp.app.APP` (the one place
replay names the reference app), `evaluate = oracle.evaluate`, `make_middleware =
gate.make_middleware`, `ledger = load_ledger_yaml(ROOT/"nemogate.yaml")`. All imported inside
the function, so replay imports and tests work before B's packages exist; a missing one raises
a RuntimeError saying which package is missing.

`Runner(backend_factory=None)` builds `ShieldedContreeSandbox(session, cwd=app.repo_root)`: the
agents' commands run from the app's repo, so a path-less `grep` or `glob` searches the repo and
not the image's default directory (docs/FACTS.md §9). A factory passed in is called with the
session only and is used as is.
