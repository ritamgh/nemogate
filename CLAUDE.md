# CLAUDE.md

@AGENTS.md

AGENTS.md above holds every project rule and command, for every agent. This file adds only what is specific to Claude Code. To change a rule, edit AGENTS.md, not this file.

## Session start

Find out who owns the session, A or B: check `CLAUDE.local.md` (gitignored, one per machine). If it isn't there, ask. Edit only that owner's folders and the shared ones.

## Backstops

`.claude/settings.json` denies reads of `.env` (rule 2), `*heldout*`, `~/Developer/nemogate-sealed/` and the original spec in `~/Downloads/` (rule 6). A denied read is the rule working. Don't route around it.

## Orchestration and workers

Execution work goes through the `orchestrate` skill. Workers don't inherit this session's context, so every worker brief must carry:

- Hard rules 1–9 from AGENTS.md, in particular `get_model(role)` only, no `.env`, the stub model only, no `*heldout*`, and no weakening of tests, predicates or ledger entries.
- **Workers never spend credit.** No `NEMOGATE_LIVE=1`, no live probes, no sweeps. Any step that needs a real model call comes back to the orchestrator, and the orchestrator asks a human first.
- The owned folders, one card per branch named `a/<component>` or `b/<component>`.
- Acceptance check: `uv run pytest` plus the generality check (AGENTS.md rule 5).
- Anything new learned about a library goes into `docs/FACTS.md` with evidence.
- A change that needs the other owner's approval (rule 4): the worker stops and reports. It does not make the change.
