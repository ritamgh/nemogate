# CLAUDE.md

@AGENTS.md

The rules above apply to every Claude session and every subagent. The notes below are specific to Claude Code.

## Session start

1. Find out who owns the session: A or B. Check `CLAUDE.local.md` (gitignored, one per machine). If it isn't there, ask. Edit only that owner's folders and the shared ones (`schemas/`, `config.yaml`), following the rules in AGENTS.md.
2. Read only the parts of `docs/SPEC.md` your task needs: Shared interfaces, plus the component and week sections. It is about 550 lines.
3. Check `docs/FACTS.md` before you use any Deep Agents, LangGraph, Contree or Token Factory API. A fact that is not marked VERIFIED is a guess.

## Sealed material

The held-out tasks (T7, T8) live outside the repo, and `*heldout*` files are gitignored. Don't search the filesystem for them, and don't read `~/Developer/nemogate-sealed/` or the original spec in `~/Downloads/`. `.claude/settings.json` denies reads of both as a backstop. The goal is a pipeline that works for any task. If a design choice only makes sense for one of the visible tasks, it's wrong.

## Secrets

`.env` holds the real keys and is gitignored. `.env.example` lists the variable names. Never read, cat, grep or print `.env`. Code loads it at runtime through `common/` and nowhere else. `.claude/settings.json` denies `Read` on it as a backstop.

## Generality check

Run it before every commit that touches pipeline code. Clean output means it passed.

```
grep -rnE '\bT[1-8]\b|C-00[0-9]|migrations/|RISK|CHANGED:|delete_file|run_tests|http_get|utils_old|descripton|export_csv' \
  common preflight ledger oracle gate replay repair report \
  --include='*.py' --include='*.yaml' --include='*.txt' --include='*.md' \
  --exclude-dir=tests --exclude-dir=fixtures 2>/dev/null
```

## Orchestration and workers

Execution work goes through the `orchestrate` skill. Every worker brief must carry these rules, because workers don't inherit this session's context:

- Hard rules 1–9 from AGENTS.md, in particular `get_model(role)` only, no `.env`, the stub model only, no `*heldout*`, and no weakening of tests, predicates or ledger entries.
- **Workers never spend credit.** No `NEMOGATE_LIVE=1`, no live probes, no sweeps. Any step that needs a real model call comes back to the orchestrator, and the orchestrator asks a human first.
- The owned folders, one card per branch named `a/<component>` or `b/<component>`.
- Acceptance check: `uv run pytest` plus the generality check above.
- Anything new learned about a library goes into `docs/FACTS.md` with evidence.

Changes to `schemas/`, the run config or `evaluate`/`run`/`make_middleware` need the other owner's approval. A worker that needs one stops and reports. It does not make the change.

## Commands

```
uv sync
uv run pytest
NEMOGATE_LIVE=1 uv run <cmd>   # spends credit: human approval first
```
