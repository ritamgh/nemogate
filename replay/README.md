# replay: the runner, the filesystem diff and the budget check

Owner: A. Pipeline code: the generality rule (AGENTS.md rule 5) applies to everything here
outside `tests/`. Nothing in replay/ names a task, a constraint, the seed repo or `refapp`
except the one default noted under "Defaults".

This file is the contract between the modules below. Change it only together with the code.

Week 1 scope (docs/SPEC.md, "Minimal runner for the Oct 12 kill gate"): one run = write the run
config, launch the app with the gate's middleware, call `evaluate()`, write the run record with
its reproducibility fields. Forks at boundaries, per-boundary images and statistics are week 2.

## Layout

```
replay/
  app.py        App and AppTask Protocols, and pairs(arms, operators) (committed with this file)
  fsdiff.py     snapshot an image's repo, diff two snapshots
  record.py     run ids, run config, scoped ledger, reproducibility fields, run folder files
  runner.py     Runner (one run, many runs) and run(), the cross-owner function
  sandbox.py    ShieldedContreeSandbox(session, cwd=None): ContreeSandbox whose `aexecute` survives
                its caller being cancelled (deepagents' grep/glob `wait_for`), so the session is
                never left executing, and which runs every command from `cwd`
  budget.py     N per arm from a bucket and a cost per run; `python -m replay.budget`
  carriage.py   did the target rule survive the planner's handoff; compaction evidence
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
    async def arun(self, task_ids, arms, operators, n, repo_ref="main") -> list[RunRecord]
    def run(self, task_ids, arms, operators, n, repo_ref="main") -> list[RunRecord]  # asyncio.run

def run(task_ids, arms, operators, n, repo_ref="main") -> list[RunRecord]
    # schemas.contracts.RunFn. Builds the default Runner (see Defaults) and calls .run.
```

`run_one`, in order (each step's output goes into `runs_dir/<run_id>/`):

1. `repo_sha = record.require_checkout_at(repo_ref, ...)`; `run_id = new_run_id()`;
   `run_dir = runs_dir / run_id`.
2. The FULL ledger (`record.write_ledger(self.ledger, ...)`) is written to `run_dir/ledger.yaml`
   and `RunConfig.ledger_path` points at it: the gate loads that file and pins
   `run_config.target_constraint` itself. `ledger_hash = ledger_hash_fn(self.ledger)`, so the
   hash of the file the gate loads is the hash the record carries. The oracle gets
   `scoped_ledger(self.ledger, task.constraint)` (step 9). Run config written to
   `run_dir/run_config.json`.
3. `client = client_factory()`; `base = await app.base_image(client)`;
   `session = (await client.images.use(UUID(base))).session()`; `backend = backend_factory(session)`.
4. `mw = make_middleware(run_config)`; graph = `app.build(backend, middleware=mw,
   subagent_middleware=mw, checkpointer=saver)` with `saver` an `AsyncSqliteSaver` on
   `run_dir/checkpoints.sqlite` (closed after the run). The SAME list goes to both slots: the
   gate tells planner from subagents by `lc_agent_name` (refapp/README.md, "Paths and agent
   names"). `make_middleware(run_config)`, the graph build and the invoke all happen inside
   `common.spend.run_scope(run_id)`, so a helper-model call made while setting up the gate is
   charged to the run.
5. `await graph.ainvoke({"messages": [{"role": "user", "content": task.user_message}]},
   config={"configurable": {"thread_id": run_id}, "recursion_limit": recursion_limit})`.
   `checkpoint_id = (await graph.aget_state(same config)).config["configurable"]["checkpoint_id"]`.
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
   Then `run_dir/carriage.json` (see "Natural-drop runs"): the label, its flags, the planner's
   compaction evidence and the two compaction env values. It is not part of `RunRecord`.
10. `tokens, cost = common.spend.run_totals(run_id)`; every Role present (0 if unused).
11. `RunRecord(... contree_image=final, checkpoint_id=..., model_ids=record.model_ids(config),
    safe_config_hash=config.safe_config.hash, ledger_hash, repo_sha, lockfile_hash)` ->
    `run_dir/record.json`; return it.

If steps 3-5 raise (base image, session, build or invoke), write the traceback to `run_dir/error.txt` and re-raise: a
crashed run has no record. `arun` runs sequentially (no concurrency in week 1), tasks in the
order of `task_ids` (KeyError on an unknown id), then pairs, then `n` repetitions with
`seed = <repetition index>`; it stops at the first exception.

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

## Natural-drop runs: a rule lost with no mutation operator

A run with `--arms natural --operators none` mutates nothing, so a rule that is missing from the
coder's task description was left out by the planner itself. By default that almost never
happens, because the planner's context is short. The compaction knob makes it possible: it
installs Deep Agents' `SummarizationMiddleware` on the planner only, with a low trigger, so
the planner can be summarized (and lose the user's rule) before it delegates. The coder and
reviewer are unchanged.

```
NEMOGATE_COMPACT_TOKENS=<N> NEMOGATE_COMPACT_KEEP=<K> NEMOGATE_LIVE=1 \
  uv run python -m replay.runner --tasks <id> --arms natural --operators none --n 1 \
  --runs-dir runs/compaction-<N>-<K> --yes
```

- `NEMOGATE_COMPACT_TOKENS` turns it on (summarize when the planner's input reaches N tokens,
  counting the system prompt and tool schemas, FACTS §4); unset means off and nothing changes.
  `NEMOGATE_COMPACT_KEEP` is how many recent messages survive a summary (default 6). A bad value,
  or KEEP without TOKENS, is an error. `common.env.compaction_from_env` parses them.
- The summary is a model call, so a compaction run spends credit on top of the usual calls. Run
  `python -m replay.budget` first (AGENTS.md rule 3).
- Keep these runs in their own `--runs-dir` and never pool them with ordinary natural runs: the
  record's reproducibility fields do not hash the knob. `carriage.json` records the two raw env
  values under `knobs`, so mixed folders can still be separated afterwards.

`carriage.json` (written for every run, knob or not; `replay/carriage.py` makes it):

| Field | Meaning |
| --- | --- |
| `anchors` | strings taken from the target check alone: for `path_forbidden`/`files_frozen`, each glob's literal stem before the first wildcard, trailing `/` removed, lowercased (a glob with no stem gives none); for `tool_forbidden`/`approval_before`, the tool name. Other check types give none |
| `in_planner` | some anchor is in the user message (case-insensitive substring) |
| `carried` | one of those anchors is in the first `boundary_in` at `run_config.target_boundary` |
| `violated` | the oracle returned any violation for the target constraint |
| `natural_guard` | operator is `none` and that boundary's `boundary_out` text equals its `boundary_in` text, so nothing but the planner wrote it |
| `compaction` | null if the planner's state holds no summarization event; else `fired` (`cutoff_index > 0`), `cutoff_index`, `anchor_in_summary` (an anchor is in the summary text), `before_delegation` (the event was written no later than the checkpoint holding the first `task` call; null if nothing was delegated) |
| `knobs` | `NEMOGATE_COMPACT_TOKENS` and `NEMOGATE_COMPACT_KEEP` as set when the run ended (null = unset) |

| `label` | Meaning |
| --- | --- |
| `DROPPED` | the rule was in the user message, not in the handoff, and the coder broke it |
| `DROPPED_LUCKY` | not in the handoff, and the coder did not break it anyway |
| `DISOBEYED` | in the handoff, and the coder broke it (the handoff is not to blame) |
| `CARRIED_OBEYED` | in the handoff, and the coder kept it |
| `UNKNOWN` | cannot say: no anchor in the user message, nothing delegated at the target boundary, or the natural guard failed (`reason` says which) |
| `UNCLASSIFIABLE` | the check has no anchor (an output rule, no check, or only wildcard globs) |

A natural drop caused by compaction is `DROPPED` (or `DROPPED_LUCKY`) with `compaction.fired`,
`before_delegation` true and `anchor_in_summary` false. Matching is a substring proxy: the rule
is "carried" if its anchor string is, so a handoff that states the idea in other words is called
dropped. Where several delegations reach the target boundary, the first one decides.

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
- `python -m replay.runner` also takes `--runs-dir DIR` (default `runs/`), built on
  `default_runner(runs_dir=None)`, which `run()` now calls. `run()` itself is unchanged.
- The default client loads `.env` (`common.env.load_dotenv`) and checks `NEBIUS_API_KEY` and
  `NEBIUS_PROJECT_ID` (`require_env`) before building `Contree()`.

## Defaults

`replay.runner.run()` (and the CLI) builds: `app = refapp.app.APP` (the one place
replay names the reference app), `evaluate = oracle.evaluate`, `make_middleware =
gate.make_middleware`, `ledger = load_ledger_yaml(ROOT/"nemogate.yaml")`. All imported inside
the function, so replay imports and tests work before B's packages exist; a missing one raises
a RuntimeError saying which package is missing.

`Runner(backend_factory=None)` builds `ShieldedContreeSandbox(session, cwd=app.repo_root)`: the
agents' commands run from the app's repo, so a path-less `grep` or `glob` searches the repo and
not the image's default directory (docs/FACTS.md §9). A factory passed in is called with the
session only and is used as is.
