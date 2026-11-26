# The rule guard and the installable package

Read when you change `Guard`, the rules file, `GuardMiddleware`, packaging, the extras, CI, or the examples. Verified against the code at `ritgh/guard` (2026-11-26). Library facts: `docs/FACTS.md` section 13.

## Package layout

The core is a pip-installable package, `nemogate`, built with hatchling (`pyproject.toml`; wheel = `src/nemogate`, test folders excluded). Subpackages: `common`, `ledger`, `oracle`, `gate`, `schemas`, `proof` (was the top-level replay folder) and `repair` (moved in too). Imports are `from nemogate.X`. The folders left at the repo root are `refapp`, `preflight`, `report`, `examples`, `scripts`, `tests`, `docs` and `results`. pytest `pythonpath` is `["src", "."]`.

- Python `>=3.11` (tested on 3.11, 3.12, 3.13). 3.11 gotcha: no backslash inside an f-string expression.
- Extras: `proof` = `contree-sdk==0.3.6` + `langgraph-checkpoint-sqlite` (the sandbox SDK imports as `contree_sdk`); `import_extra()` in `src/nemogate/proof/__init__.py` raises an ImportError naming `pip install nemogate[proof]`. `repair` is empty today (it exists so the install line stays stable). A core install must not load `nemogate.proof` or `nemogate.repair` (`src/nemogate/gate/tests/test_core_imports.py`).
- `from nemogate import Guard, GuardLoopError` (`src/nemogate/__init__.py`). Importing `nemogate` loads `nemogate.gate`.
- Source-checkout-only: `config.yaml`, `.env`, the spend log and the proof engine's paths (`record.ROOT`, runner `LEDGER_PATH`/`CONFIG_PATH`/`RUNS_DIR`, git and `uv.lock` hashing) do not work from an installed wheel. `nemogate.common.env.repo_config_path()` and `REPO_ROOT` (`parents[3]`) are the one place that knows.

## Guard API

`Guard.from_yaml(path, *, trace_path=None)` (`src/nemogate/gate/guard.py`) loads a rules file into the ledger models the oracle already reads. `.middleware()` returns the list for `create_deep_agent(middleware=...)`, and the same call goes into each `SubAgent` spec's `middleware` (the default general-purpose subagent does not get the main list: FACTS section 13). One `Guard` shares its state across all its middleware instances. It makes no model call and needs no sandbox.

- Rules file: `version: 1`, optional `approval_tool`, optional `tools:` map, `rules:` list of `{id, text, check, mode}`. Rule ids are lowercase slugs (`no-migrations`) or `C-NNN`. `mode` is `block` (default) or `observe` (log only).
- Rule types: `path_forbidden {glob, ops}`, `tool_forbidden {name}`, `approval_before {tool}` (needs the top-level `approval_tool`), `tool_arg_bound {tool, arg, op, value}` (states the allowed condition; a missing, null, mistyped, inf or NaN argument fails closed; `oracle.evaluate.arg_bound_problem` is the one comparison, used by the oracle and by `GuardMiddleware._why`).
- `tools:` maps your own file tool to `{path_arg: <argument name>, op: write|delete}` so `path_forbidden` applies to it. The argument's value may be a string or a list of strings; anything else, or a missing argument, is blocked (fail closed, `GuardMiddleware._path_problem`).
- Loader errors name the file (and rule and field): non-UTF-8, duplicate YAML keys (merge keys `<<` are not checked), empty file, unknown fields. `rules: []` loads with a warning.
- Block: the tool does not run; the result is `Blocked by rule <id>: <text>` as an error ToolMessage. The same call (same tool, args, rule) blocked 3 times in one thread (`MAX_BLOCKS`) raises `GuardLoopError`. Counters and approvals are per `thread_id` in `GuardState` (`state.py`); runs without one share the thread `default` and a warning is logged. Block-mode rule texts are appended to every `task` delegation. Optional JSONL decision trace via `trace_path`.
- Known limits are the README list ("Limits you should know"): Deep Agents only, cwd-relative case-sensitive globs, no shell parsing, in-memory counters.
- Deferred minors: `_blocks` and `_approvals` are never cleared (memory growth in a long-lived process); built-in file tools with a missing `file_path` now block and count toward `MAX_BLOCKS` (untested, not in the README); approval and call order for parallel tool calls in one turn is not exercised; `src/nemogate/repair/guards.py` does not protect files directly in `src/nemogate/`.

## Pipeline side of the same code

The same `src/nemogate/gate/middleware.py` also holds `GateMiddleware` (the experiment gate; see `docs/areas/core.md`). `src/nemogate/gate/policy.py` `_BEFORE_THE_CALL` lists the checks decided before the call: `path_forbidden`, `tool_forbidden`, `approval_before`, `tool_arg_bound`; the block_only and gated arms block `tool_arg_bound` too.

## Examples, checks and CI

- `examples/quickstart/{app.py,rules.yaml}`: the README quickstart, offline with a scripted model. `scripts/fresh_venv_check.sh` builds the wheel, installs it in a fresh venv, checks that neither `contree` nor `contree_sdk` imports, diffs the README code block against `examples/quickstart/app.py`, runs it (SC-002).
- `examples/third_party/` + `tests/test_third_party.py`: an app that takes only `Guard` (SC-001). `src/nemogate/gate/tests/test_guard_three_hop.py`: the 3-hop chain (SC-003). `src/nemogate/proof/tests/test_audit.py` scans `examples/` and exempts these two folders from the saved-run file list (size and secret scans still apply).
- `scripts/spike_311.sh` and `scripts/spike_311.py`: the day-0 spike behind FACTS section 13 (offline).
- CI: `.github/workflows/ci.yml`: matrix 3.11/3.12/3.13 (`uv sync`, ruff check, ruff format check, pytest with the 8 known golden-byte report tests deselected) plus a non-blocking `guard-latest` job that upgrades `deepagents` and runs `src/nemogate/gate`.
