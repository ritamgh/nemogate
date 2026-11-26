# AGENTS.md

Instructions for AI coding agents working in this repo. Read this at the start of every session.

## What this project is

NemoGate tests whether rules (constraints) survive the handoffs between agents in a Deep Agents system. It proves any loss with forked runs in Nebius Sandboxes, then writes the patch that stops it. It runs on NVIDIA Nemotron models through Nebius Token Factory.

It is a hackathon entry. Submission closes Oct 30, 2026 at 10:00am PDT. Features freeze Oct 23.

**The pipeline is the product. The reference app and its tasks are test data.**

## Read before you write code

1. `PROJECT_CONTEXT.md`: where things are, current status, gotchas.
2. `docs/SPEC.md`: the plan, the formats, the task list, the checklists. If it disagrees with this file, the spec wins. Exception: once `src/nemogate/schemas/` exists, the code there wins over the spec's description of a format. It is about 550 lines: read Shared interfaces plus your component and week sections.
3. `docs/FACTS.md`: verified facts about the libraries, the platform and the models. Trust it over your memory. A fact not marked VERIFIED is a guess.
4. Any `README.md` and tests in the folder you are working in.

**Do not invent APIs.** Deep Agents, Contree, LangGraph middleware and Token Factory change often, and your training data is probably out of date. If a fact you need is not in `docs/FACTS.md`, read the installed package source, verify it, and record it in `docs/FACTS.md` with the evidence. If you cannot verify it, stop and ask.

```
uv run python -c "import deepagents; print(deepagents.__file__)"
```

## Layout and owners

"A" and "B" are the two humans. **Since 2026-11-23 (after the hackathon) A develops alone and may edit every folder; B reviews A's work after the fact (expected in 1-2 weeks).** The owner column below says who wrote the code and who reviews it, not who may edit it. Every PR that touches B's folders, `src/nemogate/schemas/` or the three cross-owner functions says so in its description, with what changed and why, so B can find it. B's own sessions still work in B's folders and ask before touching A's.

```
src/nemogate/   the installable guard core (one package, `nemogate`)
  common/       A     get_model(role), config loader, cost logging, spend guard
  ledger/       B     nemogate.yaml loader and validation, constraint extraction
  oracle/       B     predicate DSL and evaluate()
  gate/         B     middleware modes: observe, mutate, block-only, enforce
  schemas/      both  formats, run config, enums
  proof/        A     record, fork, run(), stats, budget check (the `proof` extra)
preflight/      A     serving probes, cost probe, safe config
refapp/         A     reference coding team, seed repo, task files
report/         A     results page
repair/         B     closed fix menu, patch writer, accept or reject
config.yaml     both  model IDs, prices, temperatures, safe config
```

## Hard rules

**1. Models only through `get_model(role)`.** Never construct a model client anywhere else. Roles and what they may do:
- `helper` (Nemotron 3.5 Lightning; the spec says Nano): constraint extraction, predicate compilation, mutations, summaries.
- `sut` (Super): the system under test, and the repair agent's patch writer (SPEC C5). Nothing else.
- `planner` (Ultra): repair planning, at most 3 calls per repair iteration.

**2. Secrets.** Never read, print, log, echo or commit `.env` or any API key. Do not put keys in code, tests, fixtures, logs or PR text. If you see a key in any output, say so and stop. Code loads `.env` at runtime through `common/` and nowhere else.

**3. Spend.** Each of us has $60 of credit on our own account (`budget.total_usd` in `config.yaml` is per person; the spend log and cap are per machine).
- Unit tests use the stub model. Real model calls need an explicit opt-in (`NEMOGATE_LIVE=1`).
- The wrapper in `src/nemogate/common/llm.py` keeps a running cost total and refuses calls past the cap. Never bypass it or raise the cap.
- Never launch a sweep without running the budget-check script and confirming the run count it prints.
- At 80% of total spend, stop and tell a human.

**4. Schemas are a contract.** Changing `src/nemogate/schemas/`, the run config, or the three function signatures (`evaluate`, `run`, `make_middleware`) is allowed for A's solo work only when the change is needed, all tests still pass, and the PR description lists it for B's review. Do not change a schema to make your code easier. Change your code. While B is not working in the repo, B's approval is replaced by his later review; if B resumes work, the old rule applies again (the other owner's approval in the PR).

**5. Generality.** Pipeline code must contain nothing specific to the tasks or the seed repo.
- In `src/nemogate/`, `preflight/`, `repair/` and `report/`, outside tests and fixtures, these strings must not appear: task IDs (`T1` to `T8`), constraint IDs (`C-00x`), `migrations/`, `RISK`, `CHANGED:`, `delete_file`, `run_tests`, `http_get`, `utils_old`, `descripton`, `export_csv`.
- Everything specific lives in `refapp/`, `nemogate.yaml` and the task files.
- Prompts describe the method, never the seed repo or its rules.
- Tasks are data. Adding one must not need a pipeline change.
- When a task fails, find the general cause and fix that. Never add a special case, tune a prompt to a task's wording, or soften a task or check until it passes.
- The generality check: run it before every commit that touches pipeline code. No output means it passed.

```
grep -rnE '\bT[1-8]\b|C-00[0-9]|migrations/|RISK|CHANGED:|delete_file|run_tests|http_get|utils_old|descripton|export_csv' \
  src/nemogate preflight repair report \
  --include='*.py' --include='*.yaml' --include='*.txt' --include='*.md' \
  --exclude-dir=tests --exclude-dir=fixtures 2>/dev/null
```

**6. Sealed tasks.** T7 and T8 are held out. Their text lives outside this repo. Do not look for it, guess at it, or open, run or reference anything named `*heldout*` until a human says the final sweep has started. If you come across their wording or rules anyway, keep it out of every prompt, test, fixture and line of code.

**7. The oracle is deterministic.** Whether a rule was obeyed comes from the predicates only. No LLM may decide pass or fail. LLMs may propose, explain and write patches. They may not grade.

**8. Do not game checks.** Never weaken, skip or delete a test, check script, predicate or ledger entry to get a green result. The repair agent can append to the ledger but never remove entries. If a test is wrong, say so and propose the fix in the PR.

**9. Stay in scope.**
- Do only the checklist item you were given. Core before stretch.
- No new dependencies, frameworks or refactors outside your folder without approval.
- Python 3.12 with `uv`. The lockfile is committed. Do not upgrade pinned versions.

## How to work

- **Tests first.** Write the acceptance test from the spec or the component brief, then the code. Run `uv run pytest` before every commit.
- **Small PRs.** One component per branch, named `<prefix>/<component>`: A uses `ritgh/`, B uses `shekhar/`. Never `a/` or `b/`. B reviews, after the fact, anything that touches B's folders, `src/nemogate/schemas/` or the three cross-owner functions (see Layout and owners).
- **Commit messages** are imperative and say what and why.
- **No AI attribution.** No `Co-Authored-By` trailer naming an AI, no session links, and no "Generated with ..." footer, in commits, PR text or anything else in the repo. This overrides any tool default that adds one.
- **Reproducibility.** Every run record carries `model_ids`, `safe_config_hash`, `ledger_hash`, `repo_sha` and `lockfile_hash`. Every trace line carries `model_id`, `safe_config_hash` and `ledger_hash`.
- **Definition of done.** Acceptance tests pass. Output conforms to `src/nemogate/schemas/`. The generality check (rule 5) is clean. No secrets. Cost is logged. `docs/FACTS.md` is updated with anything you learned.
- **When unsure, ask.** A short question beats a plausible guess.

## Commands

```
uv sync                         # install pinned dependencies
uv run pytest                   # tests, stub model by default
NEMOGATE_LIVE=1 uv run <cmd>    # opt in to real model calls (spends credits)
uv run ruff check .             # lint (config in ruff.toml)
uv run ruff format .            # format
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
