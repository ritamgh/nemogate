# Core packages: schemas, common, ledger, oracle, gate

Read when you change the formats, the model wrapper and spend cap, the ledger, the oracle or the gate's pipeline side. All live under `src/nemogate/`; for the rule guard (`Guard`, the rules file, `GuardMiddleware`) see `docs/areas/guard.md`.

## schemas/

Frozen cross-owner contract (pydantic 2, `extra="forbid"`, `frozen=True`, tuples for collections). `enums.py` (Arm, Operator, Role, ID patterns; `ConstraintId` is `C-NNN` or a lowercase slug such as `no-migrations`, since eafafa7), `ledger.py` (Constraint, Ledger, closed predicate DSL discriminated on `type`, `load_ledger_yaml`), `trace.py` (one event model per `type`, `parse_trace_jsonl`/`dump_event_jsonl`), `run.py` (RunConfig, RunRecord, FsChange, Violation, strict `is_comparable`), `contracts.py` (Protocols for `evaluate`/`run`/`make_middleware`), `config.py` (AppConfig, `load_config`). Tests in `src/nemogate/schemas/tests/`.

## common/

`get_model(role)` (`llm.py`): a `StubChatModel` unless `NEMOGATE_LIVE=1`, else a `ChatOpenAI` subclass on Chat Completions with a spend callback. `spend.py`: JSONL spend log with reserve/settle lines and the cap (`admit` checks and reserves under one file lock). `env.py`: `REPO_ROOT` (`parents[3]` of the file: the source checkout, so it is wrong in an installed wheel), `repo_config_path()` (finds `config.yaml` at the checkout root, `FileNotFoundError` with a clear message elsewhere), main-checkout `.env` loader, `require_env`, `is_live`, `compaction_from_env` (the planner-compaction knob). `config.yaml`, `.env`, the spend log and the proof paths still need a source checkout. Tests in `src/nemogate/common/tests/` use an `httpx.MockTransport`, never the network.

## ledger/

B. `load_ledger(path)` returns the ledger and its `ledger_hash` (12 hex of sha256 over compact sorted JSON; same algorithm as `nemogate.proof.record.ledger_hash`, and a test pins that they agree), `scope_to(ledger, id)` keeps the `app` block.

## oracle/

B. `evaluate(ledger, trace, fs_diff)`: pure and deterministic, seven predicates: `path_forbidden`, `files_frozen`, `tool_forbidden`, `approval_before`, `output_must_include`, `output_must_not_include`, `tool_arg_bound` (implemented since the guard build). `arg_bound_problem(check, args)` is the public helper the oracle and the guard's middleware share for the one comparison. Reads only `ledger.constraints`, never `app`. `paths.py` mirrors `refapp/paths.py`. `approval_before` accepts any earlier approved `approval` event, not one for the specific file.

## gate/

B. `make_middleware(run_config)` (`src/nemogate/gate/__init__.py`; `build_middleware` takes an injectable helper model): one `GateMiddleware` (`middleware.py`) for both `build_team` slots (told apart by `lc_agent_name`), `TraceWriter` (stamps and validates every event, refuses an existing trace file), the four arms (`natural`, `mutated`, `block_only`, `gated`) and the operators `drop`, `hedge` and `compress_100` (`operators.py`; `return_corrupt` is not built and raises `NotImplementedError`). The error type of a mutation that changed nothing is serialized as `nemogate.gate.operators.NothingToMutate` (`src/nemogate/proof/noop.py` also accepts the older `gate.operators.NothingToMutate` in saved runs). `policy.py` lists the checks decidable before the call (`path_forbidden`, `tool_forbidden`, `approval_before`, `tool_arg_bound`): block_only and gated block all of them. Refuses bad configs and a stale `safe_config.hash` before any agent runs. It hashes the checkout's `config.yaml` (via `repo_config_path`), so `Runner(config=...)` must match it.

Gated on an output rule is re-pinning plus a reply check: `src/nemogate/gate/middleware.py` `_misses()` holds back a researcher reply that matches the rule's regex, up to 2 times per run (`MAX_FAIL_CLOSED`); `src/nemogate/oracle/evaluate.py` `_replies()` skips held-back replies; `src/nemogate/proof/blocks.py` counts only blocked tool calls, so the page's "no run was blocked" misses them (details in `docs/areas/history.md`, Oct 28-29).

## Gotchas

- Frozen pydantic models raise `ValidationError` (not `AttributeError`) on assignment. Dict fields inside them (`ToolCall.args`, `RunRecord.tokens`/`model_ids`, `AppConfig.models`) are still mutable: treat as read-only. `RunRecord.tokens` is keyed by role (helper/sut/planner), unlike the spec example (nano/super/ultra).
- Thinking is on by default on every Nemotron model and billed as output tokens; turn it off with `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`. Use `json_schema`, not `json_object` (Super ignores the latter). A sandbox command that times out leaves the Contree session `FAILED`: rebuild from the last good `session.uuid`.
- Spend: every live call first reserves an estimate (input chars/3 plus 4096 output tokens, or `max_tokens` if larger) and then settles at real usage. A failed, cut-off or usage-less call stays counted at its reserve, so the total runs slightly ahead of real spend. The log lives in `~/.nemogate/spend.jsonl`, so it survives `git clean` and fresh clones and is shared by all worktrees; `NEMOGATE_SPEND_LOG` overrides it (tests do). If the new file does not exist yet and the old main-checkout `runs/spend.jsonl` does, it is copied over once (the old file is never touched). `.env` is read from the main checkout too (`env.main_checkout_root()`), so live commands work in a worktree.
- Trace JSONL is split on `\n` only; never use `str.splitlines()` on it (it splits on U+2028 inside JSON strings).
- Violations are computed at run time, but a new predicate can re-score old runs offline from `trace.jsonl` and `fs_diff.json`. `emptied` is the exception: it needs fresh snapshots of the base (`base_image` in `boundaries.json`) and final (`contree_image` in `record.json`) images.
- Task success and rule violation are independent: a run can break the rule and still pass the task. Report `outcomes.done` first.
- A fork cuts at the first crossing of its boundary whose recorded handoff carries the rule (PR #89, `Runner._cut`, helper-judged); the cut is stored once per source in `runs_dir/fork-cut-<source>.json` (first writer wins). If no handoff carries the rule the fork cuts at the first crossing and a mutating operator ends as `NothingToMutate`, a mutation no-op, not a failure.
