# PROJECT_CONTEXT.md

The map for agents starting a session. Rules: `AGENTS.md`. Plan: `docs/SPEC.md`. Verified library facts: `docs/FACTS.md`. Detail per area: `docs/areas/`, read only the one you work in.

NemoGate tests whether rules survive handoffs between agents in a Deep Agents system, proves any loss with forked runs in Nebius Sandboxes (Contree), and writes the patch that stops it. It also ships a drop-in rule guard (`Guard`) that blocks rule-breaking tool calls. Models are NVIDIA Nemotron through Nebius Token Factory, only through `get_model(role)`.

## Status (2026-11-26)

Branch `ritgh/guard` (not yet merged to main): the core became an installable package `nemogate` under `src/nemogate/`, the guard (`from nemogate import Guard, GuardLoopError`) has four rule types, and CI runs Python 3.11 to 3.13. The hackathon work (sweeps, results page, repair rounds) is finished and written up in `docs/areas/history.md`. Known failing tests: 8 golden-byte tests in `report/tests/test_site*.py` (they predate this work; CI deselects them).

## Layout

| Path | What |
| --- | --- |
| `src/nemogate/` | The package: `common` (get_model, spend cap, env), `schemas` (frozen formats), `ledger`, `oracle`, `gate`, `proof` (runner, forks, stats, audit; was the top-level replay folder), `repair`. Imports are `from nemogate.X`. |
| `refapp/`, `preflight/`, `report/` | Reference app and tasks (test data), serving and cost probes, results page. Not in the wheel. |
| `examples/`, `tests/`, `scripts/` | Guard examples and saved runs, third-party guard test, install and spike scripts |
| `docs/` | `SPEC.md`, `FACTS.md`, `submission/`, `areas/` |
| `config.yaml`, `nemogate.yaml` | Model IDs, prices, safe config, budget; the reference ledger |
| `AGENTS.md`, `CLAUDE.md`, `.github/` | Rules, Claude Code specifics, CODEOWNERS, CI (`.github/workflows/ci.yml`) |

The gitignored runs folder holds run output; `results/` is committed.

## Commands

```
uv sync                                   # core plus dev group (proof and repair extras)
uv run pytest -q                          # stub model, offline; 8 known failures in report/
uv run ruff check . && uv run ruff format --check .
bash scripts/fresh_venv_check.sh          # wheel in a fresh venv, README quickstart runs
uv run python -m nemogate.proof.audit examples   # re-score saved runs offline
uv run python -m nemogate.proof.budget --help    # run count before any sweep
NEMOGATE_LIVE=1 uv run python -m nemogate.proof.runner --help   # spends credit
uv run python -m report.site --help       # results page
```

## Areas (read the file for the area you touch)

| Area | File | Read when |
| --- | --- | --- |
| Guard, packaging, extras, CI, examples | `docs/areas/guard.md` | changing `Guard`, the rules file, `pyproject.toml`, CI, examples |
| schemas, common, ledger, oracle, gate | `docs/areas/core.md` | changing formats, model wrapper, spend, predicates, arms, operators |
| nemogate.proof | `docs/areas/proof.md` | changing the runner, forks, stats, audit, carriage |
| refapp and tasks | `docs/areas/refapp.md` | changing the seed repo, tasks, team, research app |
| report | `docs/areas/report.md` | changing the results page, notes, publishing |
| nemogate.repair | `docs/areas/repair.md` | changing the repair agent |
| History | `docs/areas/history.md` | you need the dated sweep results or why something is as it is |

## Conventions

- Owners: A (ritgh) edits any folder since 2026-11-23; B reviews after the fact, so a PR lists changes to `ledger`, `oracle`, `gate`, `repair`, `schemas`, `config.yaml` and the signatures of `evaluate`, `run`, `make_middleware`.
- Generality check (AGENTS.md rule 5) before a commit that touches pipeline code; its folder list uses `src/nemogate`.
- Tests: stub model only; never `NEMOGATE_LIVE=1` in tests or workers. Branches `ritgh/<component>`. No AI attribution in commits.
- Python `>=3.11`; no backslash inside f-string expressions (3.11).
- `ruff` excludes `docs/` and sets `py312`, line length 100; `refapp/ruff.toml` makes the seed's `app` first-party.

## Gotchas

- `config.yaml`, `.env`, the spend log (`~/.nemogate/spend.jsonl`) and the proof paths need a source checkout; an installed wheel finds none. `REPO_ROOT` is `parents[3]` of `src/nemogate/common/env.py`.
- Editing `safe_config` in `config.yaml` means updating its `hash` (`common.llm.safe_config_hash`), or live calls refuse to start.
- Guard rules go in the main agent's middleware AND in each `SubAgent` spec (FACTS section 13); `tool_arg_bound` is implemented and blocks before the call in every arm.
- `error_type` for a no-op mutation is `nemogate.gate.operators.NothingToMutate`; `nemogate/proof/noop.py` also accepts the pre-move spelling in old runs.
- `ConstraintId` is `C-NNN` or a lowercase slug. `repair/guards.py` protects folders by path component.
- Quote `outcomes.done` first: a run can break the rule and still pass the task. Details and the other gotchas: `docs/areas/core.md`.
- Never put `heldout` in a worktree, branch, crew task or output path: `.claude/settings.json` denies reads of `*heldout*`. Use a neutral word such as `appendix`.
- The live results page was spliced, not rebuilt: `docs/areas/report.md` before touching it.
