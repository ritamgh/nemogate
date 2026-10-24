# NemoGate

NemoGate finds the rules a [LangChain Deep Agents](https://github.com/langchain-ai/deepagents) system loses at its handoffs. It proves each loss with forked runs in **Nebius Sandboxes**, and it can re-carry the rule across the handoff.

A user tells the planner agent "don't touch `migrations/`". The planner delegates to a coder subagent and paraphrases. The rule doesn't survive the handoff, and the coder edits a migration with no error anywhere. NemoGate records every handoff and every tool call, checks each rule with deterministic predicates, and compares four experimental arms on two numbers at once: how often the rule is broken, and whether the task still gets done.

## Built on Nebius Token Factory, Nemotron and Sandboxes

Every model call goes through **Nebius Token Factory** (`https://api.tokenfactory.nebius.com/v1`), using three NVIDIA Nemotron models, one per role (`config.yaml`):

| Role | Model | What it does |
| --- | --- | --- |
| `sut` (system under test) | `nvidia/nemotron-3-super-120b-a12b` | The agent team being tested: planner, coder, reviewer |
| `helper` | `nvidia/Nemotron-3_5-Lightning` | Mutations at handoffs (`drop`, `hedge`, `compress`), rule extraction, compilation into checks |
| `planner` | `nvidia/Nemotron-3-Ultra-550b-a55b` | Repair planning |

The system under test and every forked arm run inside **Nebius Sandboxes** (Contree): each run starts a fresh session from the same seed image, and a filesystem diff of the final image shows exactly what the agents changed. Unit tests, helper-model work (rule extraction, mutation, picking where to fork), scoring and the report run outside the sandboxes, and scoring is deterministic, so it can be re-checked offline from the saved trace and diff. `common/llm.py` is the only place a model client is built. It keeps a running spend total, refuses calls past the cap, and stamps a hash of the serving settings onto every trace line.

## How it works

```
user message ──▶ planner ──task──▶ coder / reviewer ──reply──▶ planner
                    │   delegation boundary      return boundary   │
                    └──────────────── gate (middleware) ───────────┘
                         records · mutates · blocks · re-pins
                                        │
                     trace.jsonl + filesystem diff ──▶ oracle ──▶ run record
```

- **Ledger** (`ledger/`, `nemogate.yaml`): the typed rules that must survive handoffs. Each rule has a scope and one check from a closed list; a rule with no check is reported as unverified, never guessed. `python -m ledger.extract` proposes new rules from a prompt with the helper model, and a human confirms or rejects each one. The ledger is append-only.
- **Oracle** (`oracle/`): `evaluate(ledger, trace, fs_diff)` checks six deterministic predicates: `path_forbidden`, `files_frozen`, `tool_forbidden`, `approval_before`, `output_must_include` and `output_must_not_include`. No language model ever decides pass or fail.
- **Gate** (`gate/`): one Deep Agents middleware placed in the planner and in every subagent. It records every handoff and tool call as a validated trace line, and in each arm:

  | Arm | At the target handoff | Tool calls |
  | --- | --- | --- |
  | `natural` | unchanged | recorded |
  | `mutated` | operator applied (`drop`, `hedge`, `compress_100`) | recorded |
  | `block_only` | operator applied | calls the oracle would flag are blocked; the rule is not re-sent |
  | `gated` (NemoGate) | operator applied, then the rule re-pinned | blocked as above; replies that miss an output rule are held back so the planner re-delegates |

  Operators run on delegation boundaries (planner → subagent) and on return boundaries (subagent → planner).
- **Replay** (`replay/`): the runner writes the run config, starts the team from the seed image with the gate, computes the filesystem diff and the task check, calls the oracle, and writes a run record. The record carries the model ids, the safe-config hash, the ledger hash, the repo commit and the lockfile hash, so only runs made under identical conditions are ever pooled.
- **Reference app** (`refapp/`): a small Flask + sqlite3 task tracker and a Deep Agents coding team (planner, coder, reviewer). It's test data: nothing in the pipeline names it, and adding a task is a data-only change.

## Related work

- **[Lost in Context Compaction](https://arxiv.org/abs/2608.11242)** (arXiv 2608.11242) is the closest work. It measures what happens to a user's standing instructions ("side constraints") when an agent compacts its own context. The compactors it tests kept 17% of injected constraints on average. In its inbox agent, deleting emails without approval went from 0 of 100 sessions before compaction to 30 of 100 after. Its fix is an extractor that appends the constraints to the compacted context, and restating the constraint cut the inbox violations to 13 of 100, not to zero. It studies one agent's own context, not handoffs between agents.
  NemoGate looks at the other place a rule can be lost: the delegation and return boundaries of a multi-agent team. It proves a loss with forked runs, grades obedience with a deterministic oracle over tool calls, agent replies and the filesystem diff, and re-pins the rule at the handoff in the `gated` arm. The mechanism is similar to theirs, so the new part is where it runs and how it is graded.
- **[MAST](https://arxiv.org/abs/2503.13657)** (arXiv 2503.13657) labels failures in over 1,600 multi-agent traces with 14 failure modes, including "disobey task specification". It labels traces after the fact. NemoGate tests one failure, rule loss at a handoff, by changing the handoff and re-running.
- **[Who&When](https://arxiv.org/abs/2505.00212)** (arXiv 2505.00212) asks which agent and which step caused a failure in a multi-agent trace. It attributes failures. It does not test whether a given rule was lost at a handoff.

## Running it

```bash
uv sync
uv run pytest                      # offline: stub models, no credit spent
```

Live runs need `NEBIUS_API_KEY` and `NEBIUS_PROJECT_ID` in `.env` (see `.env.example`; never commit it) and an explicit opt-in:

```bash
uv run python -m replay.budget --bucket kill_gate --arms natural mutated \
    --operators none drop --tasks T1 T6 --from-runs runs     # how many runs the budget allows
NEMOGATE_LIVE=1 uv run python -m replay.runner --tasks T1 T6 \
    --arms natural mutated --operators none drop --n N --yes
NEMOGATE_LIVE=1 uv run python -m ledger.extract --ledger nemogate.yaml \
    --text-file PROMPT.txt --agents planner,coder,reviewer --tools write_file,delete,http_get --dry-run
```

## Repository

| Folder | What |
| --- | --- |
| `schemas/` | The shared formats: ledger, trace events, run config and record, app config |
| `common/` | `get_model(role)`, the spend log and cap, `.env` loading |
| `ledger/`, `oracle/`, `gate/` | The rules, the deterministic checks, the middleware |
| `replay/` | The runner, the filesystem diff, the budget check |
| `refapp/` | The reference app and its tasks (test data) |
| `docs/` | `SPEC.md` (the plan and its change log), `FACTS.md` (verified facts about the libraries, the platform and the models, with evidence) |

Licensed under Apache-2.0 (`LICENSE`).
