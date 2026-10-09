# PROJECT_CONTEXT.md

A map for agents starting a session. The rules are in `AGENTS.md`, the plan is in `docs/SPEC.md`, and verified library facts are in `docs/FACTS.md`. This file only says where things are.

## Status (2026-10-09)

The repo has docs and config only, no code yet. Next: the day-0 spike (FACTS sections 1–5), then A's week-1 skeleton (`pyproject.toml`, `uv.lock`, `schemas/`, lint), with B on the interface freeze.

## Files

| Path | What |
| --- | --- |
| `AGENTS.md` | Rules for every agent: hard rules, owners, vocabulary. Read it first. |
| `CLAUDE.md` | Imports AGENTS.md and adds Claude Code notes: generality grep, worker brief rules |
| `CLAUDE.local.md` | Per machine, gitignored: which owner (A or B) this machine's sessions belong to |
| `docs/SPEC.md` | Build spec: formats, cross-owner signatures, tasks T1–T6, weekly checklists, budget. The held-out T7 and T8 are sealed outside the repo. |
| `docs/FACTS.md` | Verified API, platform and model facts, with evidence. Mostly NOT YET CHECKED. |
| `.env.example` | Names of the env variables. The real `.env` is gitignored and agents never read it. |
| `.claude/settings.json` | Shared Claude Code permissions (deny reading `.env` and `*heldout*`) |
| `ruff.toml` | Lint and format config (ruff, py312, line length 100) |
| `.github/CODEOWNERS` | Shared files need the other owner's approval: `main` is protected and requires a PR with code-owner review |

## Planned layout (from SPEC "Repo layout")

`common/ preflight/ refapp/ replay/ report/` (A), `ledger/ oracle/ gate/ repair/` (B), `schemas/` and `config.yaml` (shared). `runs/` is gitignored and `results/` is committed.

## Gotchas

- SQLite checkpoints (`*.sqlite`, `*.db`) and `runs/` are gitignored. If the reference app ever needs a committed seed DB, add an exception in `.gitignore`.
