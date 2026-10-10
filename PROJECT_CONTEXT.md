# PROJECT_CONTEXT.md

A map for agents starting a session. The rules are in `AGENTS.md`, the plan is in `docs/SPEC.md`, and verified library facts are in `docs/FACTS.md`. This file only says where things are.

## Status (2026-10-10)

The uv skeleton (`pyproject.toml`, `uv.lock`, Python 3.12, `package = false`, pytest `pythonpath=["."]`) and the shared `schemas/` exist (PRs #5 and #7, merged). The spike libraries are pinned (`deepagents`, `langgraph`, `langgraph-checkpoint-sqlite`, `langchain-openai`, `contree-sdk`). The offline half of the day-0 spike is done: FACTS sections 1, 3, 4 and 5 are verified from installed source and fake-model runs, and §9 lists the gotchas. FACTS §2 has the model IDs, prices and limits from the free models list; the helper is Nemotron 3.5 Lightning (PR #10). `common/llm.py` and the spend cap exist (branch `ritgh/llm`). The live spike ran on 2026-10-10 ($0.024): FACTS §2–5 are now verified live (structured output, tool calls, thinking switch, sandbox limits, a planner + subagent run in Contree with both forks). The safe config is set (FACTS §6): `json_schema`, thinking off for every role, hash `8c2f708f9c34`; `get_model` sends the thinking switch and refuses a stale hash. Next: the reference app, task check scripts, the minimal runner (the C0 probes are still scripts outside the repo, not a `preflight/` module).

## Files

| Path | What |
| --- | --- |
| `AGENTS.md` | Rules for every agent: hard rules, owners, vocabulary. Read it first. |
| `CLAUDE.md` | Imports AGENTS.md and adds only Claude Code specifics: owner check, `settings.json` backstops, worker-brief rules. Rules and commands live in AGENTS.md only. |
| `CLAUDE.local.md` | Per machine, gitignored: which owner (A or B) this machine's sessions belong to |
| `docs/SPEC.md` | Build spec: formats, cross-owner signatures, tasks T1–T6, weekly checklists, budget. The held-out T7 and T8 are sealed outside the repo. |
| `docs/FACTS.md` | Verified API, platform and model facts, with evidence. §1 and §3–5 are filled; §2 (Token Factory) and the "needs live" rows wait for the live spike. §9 holds the gotchas. |
| `schemas/` | Frozen cross-owner contract (pydantic 2, `extra="forbid"`, `frozen=True`, tuples for collections). `enums.py` (Arm, Operator, Role, ID patterns), `ledger.py` (Constraint, Ledger, closed predicate DSL discriminated on `type`, `load_ledger_yaml`), `trace.py` (one event model per `type`, `parse_trace_jsonl`/`dump_event_jsonl`), `run.py` (RunConfig, RunRecord, FsChange, Violation, strict `is_comparable`), `contracts.py` (Protocols for `evaluate`/`run`/`make_middleware`), `config.py` (AppConfig, `load_config`). Tests in `schemas/tests/`. |
| `common/` | `get_model(role)` (`llm.py`): a `StubChatModel` unless `NEMOGATE_LIVE=1`, else a `ChatOpenAI` subclass on Chat Completions with a spend callback. `spend.py`: JSONL spend log with reserve/settle lines and the cap (`admit` checks and reserves under one file lock). `env.py`: `.env` loader, `require_env`, `is_live`. Tests in `common/tests/` use an `httpx.MockTransport`, never the network. |
| `config.yaml` | Model IDs, prices, temperatures, safe config, budget. `null` = not yet filled (SUT temperature). Editing `safe_config` means updating its `hash` too (`common.llm.safe_config_hash`), or live calls refuse to start |
| `.env.example` | Names of the env variables. The real `.env` is gitignored and agents never read it. |
| `.claude/settings.json` | Shared Claude Code permissions (deny reading `.env` and `*heldout*`) |
| `ruff.toml` | Lint and format config (ruff, py312, line length 100) |
| `.github/CODEOWNERS` | Shared files need the other owner's approval: `main` is protected and requires a PR with code-owner review |

## Planned layout (from SPEC "Repo layout")

`common/ preflight/ refapp/ replay/ report/` (A), `ledger/ oracle/ gate/ repair/` (B), `schemas/` and `config.yaml` (shared). `runs/` is gitignored and `results/` is committed.

## Gotchas

- Frozen pydantic models raise `ValidationError` (not `AttributeError`) on assignment. Dict fields inside them (`ToolCall.args`, `RunRecord.tokens`/`model_ids`, `AppConfig.models`) are still mutable: treat as read-only.
- `RunRecord.tokens` is keyed by role (helper/sut/planner), unlike the spec example (nano/super/ultra).
- Thinking is on by default on every Nemotron model and billed as output tokens; turn it off with `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`. Use `json_schema`, not `json_object` (Super ignores the latter). A sandbox command that times out leaves the Contree session `FAILED`: rebuild from the last good `session.uuid`.
- Spend: every live call first reserves an estimate (input chars/3 plus 4096 output tokens, or `max_tokens` if larger) and then settles at real usage. A failed, cut-off or usage-less call stays counted at its reserve, so the total runs slightly ahead of real spend. The log lives in the main checkout's `runs/spend.jsonl`, shared by all worktrees; `NEMOGATE_SPEND_LOG` overrides it (tests do).
- Trace JSONL is split on `\n` only; never use `str.splitlines()` on it (it splits on U+2028 inside JSON strings).
- ruff excludes `docs/` (ruff 0.16 would reformat code blocks in the spec).

- SQLite checkpoints (`*.sqlite`, `*.db`) and `runs/` are gitignored. If the reference app ever needs a committed seed DB, add an exception in `.gitignore`.
