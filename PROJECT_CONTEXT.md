# PROJECT_CONTEXT.md

A map for agents starting a session. The rules are in `AGENTS.md`, the plan is in `docs/SPEC.md`, and verified library facts are in `docs/FACTS.md`. This file only says where things are.

## Status (2026-10-10)

The uv skeleton (`pyproject.toml`, `uv.lock`, Python 3.12, `package = false`, pytest `pythonpath=["."]`) and the shared `schemas/` exist (PRs #5 and #7, merged). The spike libraries are pinned (`deepagents`, `langgraph`, `langgraph-checkpoint-sqlite`, `langchain-openai`, `contree-sdk`). The offline half of the day-0 spike is done: FACTS sections 1, 3, 4 and 5 are verified from installed source and fake-model runs, and §9 lists the gotchas. Next: the live half (FACTS §2, plus the "needs live" rows in §3–5), then `common/llm.py`.

## Files

| Path | What |
| --- | --- |
| `AGENTS.md` | Rules for every agent: hard rules, owners, vocabulary. Read it first. |
| `CLAUDE.md` | Imports AGENTS.md and adds only Claude Code specifics: owner check, `settings.json` backstops, worker-brief rules. Rules and commands live in AGENTS.md only. |
| `CLAUDE.local.md` | Per machine, gitignored: which owner (A or B) this machine's sessions belong to |
| `docs/SPEC.md` | Build spec: formats, cross-owner signatures, tasks T1–T6, weekly checklists, budget. The held-out T7 and T8 are sealed outside the repo. |
| `docs/FACTS.md` | Verified API, platform and model facts, with evidence. §1 and §3–5 are filled; §2 (Token Factory) and the "needs live" rows wait for the live spike. §9 holds the gotchas. |
| `schemas/` | Frozen cross-owner contract (pydantic 2, `extra="forbid"`, `frozen=True`, tuples for collections). `enums.py` (Arm, Operator, Role, ID patterns), `ledger.py` (Constraint, Ledger, closed predicate DSL discriminated on `type`, `load_ledger_yaml`), `trace.py` (one event model per `type`, `parse_trace_jsonl`/`dump_event_jsonl`), `run.py` (RunConfig, RunRecord, FsChange, Violation, strict `is_comparable`), `contracts.py` (Protocols for `evaluate`/`run`/`make_middleware`), `config.py` (AppConfig, `load_config`). Tests in `schemas/tests/`. |
| `config.yaml` | Model IDs, prices, temperatures, safe config, budget. `null` = not yet filled by the spike |
| `.env.example` | Names of the env variables. The real `.env` is gitignored and agents never read it. |
| `.claude/settings.json` | Shared Claude Code permissions (deny reading `.env` and `*heldout*`) |
| `ruff.toml` | Lint and format config (ruff, py312, line length 100) |
| `.github/CODEOWNERS` | Shared files need the other owner's approval: `main` is protected and requires a PR with code-owner review |

## Planned layout (from SPEC "Repo layout")

`common/ preflight/ refapp/ replay/ report/` (A), `ledger/ oracle/ gate/ repair/` (B), `schemas/` and `config.yaml` (shared). `runs/` is gitignored and `results/` is committed.

## Gotchas

- Frozen pydantic models raise `ValidationError` (not `AttributeError`) on assignment. Dict fields inside them (`ToolCall.args`, `RunRecord.tokens`/`model_ids`, `AppConfig.models`) are still mutable: treat as read-only.
- `RunRecord.tokens` is keyed by role (helper/sut/planner), unlike the spec example (nano/super/ultra).
- Trace JSONL is split on `\n` only; never use `str.splitlines()` on it (it splits on U+2028 inside JSON strings).
- ruff excludes `docs/` (ruff 0.16 would reformat code blocks in the spec).

- SQLite checkpoints (`*.sqlite`, `*.db`) and `runs/` are gitignored. If the reference app ever needs a committed seed DB, add an exception in `.gitignore`.
