# AGENTS.md

Instructions for AI coding agents working in this repo. Read this at the start of every session.

## What this project is

NemoGate tests whether rules (constraints) survive the handoffs between agents in a Deep Agents system. It proves any loss with forked runs in Nebius Sandboxes, then writes the patch that stops it. It runs on NVIDIA Nemotron models through Nebius Token Factory.

It is a hackathon entry. Submission closes Oct 30, 2026 at 10:00am PDT. Features freeze Oct 23.

**The pipeline is the product. The reference app and its tasks are test data.**

## Read before you write code

1. `docs/SPEC.md`: the plan, the formats, the task list, the checklists. If it disagrees with this file, the spec wins. Exception: once `schemas/` exists, the code there wins over the spec's description of a format.
2. `docs/FACTS.md`: verified facts about the libraries, the platform and the models. Trust it over your memory.
3. Any `README.md` and tests in the folder you are working in.

**Do not invent APIs.** Deep Agents, Contree, LangGraph middleware and Token Factory change often, and your training data is probably out of date. If a fact you need is not in `docs/FACTS.md`, read the installed package source, verify it, and record it in `docs/FACTS.md` with the evidence. If you cannot verify it, stop and ask.

```
uv run python -c "import deepagents; print(deepagents.__file__)"
```

## Layout and owners

"A" and "B" are the two humans. Work in the folders owned by whoever started your session. If you need a change in the other owner's folder, say so instead of making it.

```
common/     A     get_model(role), config loader, cost logging, spend guard
preflight/  A     serving probes, cost probe, safe config
refapp/     A     reference coding team, seed repo, task files
replay/     A     record, fork, run(), stats, budget check
report/     A     results page
ledger/     B     nemogate.yaml loader and validation, constraint extraction
oracle/     B     predicate DSL and evaluate()
gate/       B     middleware modes: observe, mutate, block-only, enforce
repair/     B     closed fix menu, patch writer, accept or reject
schemas/    both  formats, run config, enums
config.yaml both  model IDs, prices, temperatures, safe config
```

## Hard rules

**1. Models only through `get_model(role)`.** Never construct a model client anywhere else. Roles and what they may do:
- `helper` (Nano): constraint extraction, predicate compilation, mutations, summaries.
- `sut` (Super): the system under test, nothing else.
- `planner` (Ultra): repair planning, at most 3 calls per repair iteration.

**2. Secrets.** Never read, print, log, echo or commit `.env` or any API key. Do not put keys in code, tests, fixtures, logs or PR text. If you see a key in any output, say so and stop.

**3. Spend.** We have about $30 of credit in total.
- Unit tests use the stub model. Real model calls need an explicit opt-in (`NEMOGATE_LIVE=1`).
- The wrapper in `common/llm.py` keeps a running cost total and refuses calls past the cap. Never bypass it or raise the cap.
- Never launch a sweep without running the budget-check script and confirming the run count it prints.
- At 80% of total spend, stop and tell a human.

**4. Schemas are frozen after Oct 9.** Do not change `schemas/`, the run config, or the three cross-owner function signatures (`evaluate`, `run`, `make_middleware`) without the other owner's approval in the PR. Do not change a schema to make your code easier. Change your code.

**5. Generality.** Pipeline code must contain nothing specific to the tasks or the seed repo.
- In `common/`, `preflight/`, `ledger/`, `oracle/`, `gate/`, `replay/`, `repair/` and `report/`, outside tests and fixtures, these strings must not appear: task IDs (`T1` to `T8`), constraint IDs (`C-00x`), `migrations/`, `RISK`, `CHANGED:`, `delete_file`, `run_tests`, `http_get`, `utils_old`, `descripton`, `export_csv`.
- Everything specific lives in `refapp/`, `nemogate.yaml` and the task files.
- Prompts describe the method, never the seed repo or its rules.
- Tasks are data. Adding one must not need a pipeline change.
- When a task fails, find the general cause and fix that. Never add a special case, tune a prompt to a task's wording, or soften a task or check until it passes.

**6. Sealed tasks.** T7 and T8 are held out. Their text lives outside this repo. Do not look for it, guess at it, or open, run or reference anything named `*heldout*` until a human says the final sweep has started. If you come across their wording or rules anyway, keep it out of every prompt, test, fixture and line of code.

**7. The oracle is deterministic.** Whether a rule was obeyed comes from the predicates only. No LLM may decide pass or fail. LLMs may propose, explain and write patches. They may not grade.

**8. Do not game checks.** Never weaken, skip or delete a test, check script, predicate or ledger entry to get a green result. The repair agent can append to the ledger but never remove entries. If a test is wrong, say so and propose the fix in the PR.

**9. Stay in scope.**
- Do only the checklist item you were given. Core before stretch.
- No new dependencies, frameworks or refactors outside your folder without approval.
- Python 3.12 with `uv`. The lockfile is committed. Do not upgrade pinned versions.

## How to work

- **Tests first.** Write the acceptance test from the spec or the component brief, then the code. Run `uv run pytest` before every commit.
- **Small PRs.** One component per branch (`a/<component>` or `b/<component>`). The other owner reviews anything that touches `schemas/` or the three cross-owner functions.
- **Commit messages** are imperative and say what and why.
- **Reproducibility.** Every run record carries `model_ids`, `safe_config_hash`, `ledger_hash`, `repo_sha` and `lockfile_hash`. Every trace line carries `model_id`, `safe_config_hash` and `ledger_hash`.
- **Definition of done.** Acceptance tests pass. Output conforms to `schemas/`. The generality check is clean. No secrets. Cost is logged. `docs/FACTS.md` is updated with anything you learned.
- **When unsure, ask.** A short question beats a plausible guess.

## Commands

```
uv sync                         # install pinned dependencies
uv run pytest                   # tests, stub model by default
NEMOGATE_LIVE=1 uv run <cmd>    # opt in to real model calls (spends credits)
# TODO: lint and format commands, once chosen
```

## Vocabulary

- **Boundary:** a point where context is compressed and passed on, for example `delegation:planner->coder` or `return:coder->planner`.
- **Constraint (rule):** something that must survive a boundary and be obeyed. It has an ID like `C-001`.
- **Ledger:** `nemogate.yaml`, the typed list of constraints.
- **Arm:** `natural`, `mutated`, `block_only` or `gated`.
- **Operator:** a deliberate sabotage of the handoff: `drop`, `hedge`, `compress_100`.
- **Oracle:** deterministic predicates that decide whether a constraint was violated.
- **Gate:** the middleware that observes, mutates, blocks or re-pins constraints at boundaries.
- **A run's ledger holds only its `target_constraint`.** The gate pins that entry and the oracle checks it, and no other.
