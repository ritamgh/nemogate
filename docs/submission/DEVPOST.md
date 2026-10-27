# NemoGate: Devpost submission text

Draft for the Devpost form. Paste each section into the matching field. Every number comes from the
[results page](https://ritamgh.github.io/nemogate/) (republished 2026-10-21) unless it is marked
otherwise; the table at the end says where each one is. **Re-check them against the page you
submit with** (the "Before you submit" list is the last section).

---

## Basics

| Field | Value |
| --- | --- |
| Project name | NemoGate |
| Track | Coding and Agentic Engineering |
| Tagline | Does your agent team keep the rules you give it? NemoGate finds where a rule dies between agents, proves it with forked re-runs, and carries it across. |
| Demo (hosted) | https://ritamgh.github.io/nemogate/ |
| Code (public, Apache-2.0) | https://github.com/ritamgh/nemogate |
| Video | *(YouTube link, under 3 minutes)* |
| Built with | Nebius Token Factory, NVIDIA Nemotron 3 (Lightning, Super, Ultra), Nebius Sandboxes (Contree), LangChain Deep Agents, Tavily |
| Team | Ritam Ghosh ([@ritamgh](https://github.com/ritamgh)), Shekhar Padhy ([@Shekharpadhy](https://github.com/Shekharpadhy)) |

## What we built during the submission period

NemoGate did not exist before this hackathon. The first commit is dated 2026-10-09, inside the
submission period (Aug 26 to Oct 30, 2026), and the whole project is new.

---

## The problem, in one example

A user tells a planner agent: *"Don't touch `migrations/`."* The planner hands the work to a coder
subagent and restates the task in its own words. In Deep Agents' default subagent mode the coder sees
only that restatement. If the sentence about `migrations/` was dropped or softened on the way, the
coder edits a migration. Nothing errors. The task even passes its tests.

NemoGate turns that failure into something you can measure.

## What it does

1. **A ledger of rules.** Each rule is a typed entry with a scope and one check from a closed list of
   six (`path_forbidden`, `files_frozen`, `tool_forbidden`, `approval_before`, `output_must_include`,
   `output_must_not_include`). A rule with no check is reported as unverified, never guessed.
2. **A gate**, one Deep Agents middleware placed in the planner and in every subagent. It records
   every handoff and tool call. In the experiment it can sabotage the handoff (`drop` the rule,
   `hedge` it, `compress` the message), block calls the oracle would flag, or re-pin the rule into
   the handoff.
3. **A deterministic oracle.** Whether a rule was kept is decided by programs over the trace and the
   filesystem diff. No language model ever grades a run.
4. **Forked re-runs in Nebius Sandboxes.** A run is recorded, then forked at the handoff, so each arm
   sees the same prefix and only the handoff differs.
5. **Four arms:** `natural` (nothing changed), `mutated` (the handoff is sabotaged), `block_only`
   (calls the oracle would flag are refused; the rule is not re-sent) and `gated` (refused, and the
   rule is put back into the handoff).
6. **A results page** that reports, for every rule, whether the agents *did the job without breaking
   the rule*, with exact intervals and exact tests, and says what the numbers cannot tell you.
7. **A repair agent** that proposes a fix from a closed menu, writes the patch, re-runs it and
   accepts or rejects it by a fixed rule.
8. **An offline audit** (`uv run python -m replay.audit examples`) that re-scores three saved runs
   from their evidence with no API key, no network and no model.

---

## Technological Implementation

**Three Nemotron models on one Token Factory endpoint, each with a job** (`config.yaml`,
`common/llm.py` is the only place a model client is built):

| Role | Model | Job | Price per 1M tokens (in / out) |
| --- | --- | --- | --- |
| system under test | `nvidia/nemotron-3-super-120b-a12b` | the planner, coder and reviewer being tested; also writes repair patches | $0.30 / $0.90 |
| helper | `nvidia/Nemotron-3_5-Lightning` | sabotages handoffs, proposes and compiles rules into checks | $0.06 / $0.24 |
| planner | `nvidia/Nemotron-3-Ultra-550b-a55b` | chooses a repair fix, at most 3 calls per iteration | $1.00 / $3.00 |

**Every run executes in Nebius Sandboxes** (Contree): a fresh session from the same seed image, and a
diff of the final image shows what the agents changed. A fork is a new session on a snapshot, about
1.3 seconds at the median (10 forks at once took 6.3 seconds with no rate-limit retries).

**Choices that came from measuring the platform** (details in our feedback writeup):

- Nemotron's thinking is on by default and billed as output tokens. Turning it off cut output tokens
  3 to 16 times and latency 2 to 10 times with no loss of valid output, so it is off for every role.
- Super ignored `response_format: json_object` (0 of 3 valid) and obeyed `json_schema` (3 of 3, and
  24 of 24 across the escape probes), so every structured call pins `json_schema`.
- Tool calls came back as native `tool_calls`; a scan of saved runs found none written as text in
  1,474 AI messages over 36 live runs.

**Engineering for trust:**

- Spend is capped in code (`common/llm.py` refuses calls past the cap); a budget check prints the
  run count before any sweep.
- Every run record carries the model ids, a hash of the serving settings, the ledger hash, the repo
  commit and the lockfile hash. Runs made under different conditions are never pooled.
- The pipeline names no task or rule: everything specific lives in `refapp/`, `nemogate.yaml` and the
  task files. A grep for task ids and rule wording over the pipeline folders must print nothing.
- Two tasks were kept **sealed outside the repo** and not opened until the final sweep, so the
  pipeline could not be fitted to them.
- More than 2,400 offline tests (stub models, no credit spent).

---

## Design

NemoGate is a pipeline with one flow, not a script: **rules in → handoffs recorded → rule checked →
runs compared → report → patch.**

- **A results page you can read cold.** It opens on an "At a glance" table: for each rule, how often
  it was broken with no gate, with block-only and with the gate, how often the agent *tried* the
  forbidden action, and the exact-test p-value for each comparison. Missing data shows a dash, never 0.
- **It states its own limits** on the page: blocking applies only to path, tool and approval rules;
  forked cells share a source run; held-out cells have 5 to 6 runs.
- **A replay page** that tells one rule's life in four steps from saved runs: the rule, the handoff
  with the sentence struck out, the action that broke it, and the gated run beside it.
- **Anyone can check a verdict offline.** `replay.audit` re-scores saved runs from their own files.
- **Adding a task is a data change.** We proved it with a second app: a research team (a lead agent
  plus a researcher with Tavily web search) runs through the same pipeline with one new ledger rule
  and no pipeline change.

---

## Potential Impact

Teams are putting agents in front of real systems and giving them standing instructions: don't touch
this folder, ask before deleting, never cite this source. Those instructions are exactly what a
paraphrasing planner can lose, and a loss is silent. We give builders a way to **measure** whether
their rules survive and a gate that keeps them alive.

**What the evidence says** (Nemotron 3 Super as the agent team, default subagent mode):

| What we measured | Result |
| --- | --- |
| Rule lost at the handoff (T6, `migrations/` rule) | broken in **9 of 14** runs (64%) against **4 of 15** (27%) with no sabotage: **+38 points** (95% interval +2 to +63) |
| Blocking alone | stopped the violations it can see (0 of 14 broken), but the agent kept trying: it **tried the forbidden action in 11 of 14** runs |
| Carrying the rule too (gated) | attempts fell to **4 of 14**; exact test **p = 0.0213** |
| Same on "ask before deleting" (T10) | attempts **10 of 20 → 3 of 20**; exact test on matched forks **p = 0.0156** (7 pairs where only block-only tried, none where only gated did) |
| Same on "don't modify `app/serializers.py`" (T11) | **9 of 17 → 4 of 16**; p = 0.0625, **not significant** |
| Second app: research team with Tavily (T14, rule: don't cite Wikipedia) | gated broke the rule in **0 of 33** runs, block-only in **10 of 32**; the paired test on matched forks is the headline: 0 against 8 over 29 pairs, **p = 0.0078** (see the notes below the table) |

**Notes on the second app (T14).**

- **Errors.** 21 of 160 runs errored (20 "System is overloaded" 529 responses from the server and one timeout): block-only 8, gated 7, mutated 6,
  natural 0. They are left out of the counts above; 29 matched pairs remain from 40 repetitions.
- **Why T14 exists.** T13 was the first research task. Its base rate came out low, so we wrote T14
  under the same rule. The check and the rule are unchanged; T14 only requires a link after every
  claim. T13 pilot, never pooled with T14: rule dropped (mutated) broke it in 1 of 8 runs, block-only
  1 of 5, gated 0 of 5, natural 1 of 12 (7 of 37 runs errored).

**What it does not show, and we say so:**

- Blocking already gives 0 violations on rules the gate can check before the call, by construction.
  The gate's measured value is **fewer attempts**, not fewer violations.
- Gated did not finish the job significantly more often than block-only (T6: 10 of 14 against 7 of
  14, p = 0.44). Intervals are wide.
- On two sealed held-out tasks, the rule that blocking cannot enforce (graded after the run) was
  run twice. In the **first run** gated broke it in 1 of 5 and block-only in 4 of 5. We then fixed how
  forks are scored (PR #89) and **re-ran**: block-only broke it in 4 of 5 (no sabotage) and 5 of 5
  (rule dropped) runs and gated in 0 of 5 and 0 of 5. The re-run came after that fix. Over the 10 matched fork pairs the exact test gives p = 0.0039, **but the 10 pairs come
  from only 5 sources (each forked under both operators), so they are not independent; counting each
  source once, all 5 point the same way and p = 0.0625.** We treat it as the direction we expected
  and as exploratory, not as a significant result.
- A repair that only edits the planner's prompt did not hold: violations 45% → 44%, task success
  90% → 88% on T11, so the accept rule rejected it. The gate, not a prompt patch, is what worked.
- One agent model, two apps, small samples, and Deep Agents' default `isolated` subagent mode. Deep
  Agents also has an experimental `fork` mode that shares the parent's conversation; we have not
  measured it.

**Cost of prevention.** A run costs about $0.12 to $0.19 (T6: $0.161 natural, $0.115 mutated,
$0.188 block-only, $0.150 gated). For the one rule that can be priced (T6, 14 runs per arm), the
results page's cost table gives about **$0.054 per prevented violation** for gated. The widest
reading runs from "may cost less" up to $0.61, and the data cannot rule out that it cost nothing.

---

## Quality of the Idea

- **A rule as a thing you can test.** Most agent tooling observes what happened or filters what a
  model says. NemoGate asks a different question: *did this specific user instruction survive this
  specific handoff?* It answers it by changing the handoff and re-running from the same point.
- **Why not let a model grade?** An LLM that grades another model's compliance adds a second source
  of error and is not repeatable. The oracle is deterministic and a saved run can be re-scored by
  anyone.
- **Why not just block the tool call?** We tested exactly that, with its own arm. Blocking is
  necessary for rules it can see and does nothing for rules it cannot (an output that must contain a
  line, a file that must stay unchanged). Carrying the rule helps the agent stop trying.
- **Using Nemotron against itself.** The cheapest Nemotron (Lightning) is the saboteur and rule
  extractor, the middle one (Super) is the team under test, and the largest (Ultra) plans repairs.
  The helper never edits text freely: it names which sentences carry the rule, and code removes them,
  because asking it to rewrite a delegation freely returned the delegation unchanged in 3 of 3 probes.
- **Honest negative results are part of the product.** The page reports a rejected repair, a
  non-significant effect and the cells where blocking and carrying are indistinguishable.

---

## How to try it

```bash
git clone https://github.com/ritamgh/nemogate && cd nemogate
uv sync
uv run pytest                                    # offline, stub models, no credit
uv run python -m replay.audit examples           # re-score three saved runs, no API key
```

Live runs need `NEBIUS_API_KEY` and `NEBIUS_PROJECT_ID` (and `TAVILY_API_KEY` for the research app) in
`.env`, plus `NEMOGATE_LIVE=1`; the README has the commands and the budget check that comes first.

## Feedback

Our feedback on Token Factory, Sandboxes and the NVIDIA models is in
[`docs/submission/FEEDBACK.md`](FEEDBACK.md).

---

## Where each number comes from

| Number | Where |
| --- | --- |
| T6: 4 of 15, 9 of 14, +38 points (+2 to +63) | results page, "Don't modify anything under migrations/", rule-effect line |
| T6: attempts 11 of 14 → 4 of 14, p = 0.0213; did the job 10 of 14 vs 7 of 14, p = 0.44 | results page, same section, exact-test table |
| T6 cost per run | results page, same section, cost table |
| T10: 10 of 20 → 3 of 20, p = 0.0156; T11: 9 of 17 → 4 of 16, p = 0.0625 | results page, "At a glance" |
| Held-out: 0 of 5 vs 4 of 5 (p = 0.125), 0 of 5 vs 5 of 5 (p = 0.0625), pooled 0 of 10 vs 9 of 10 pairs (p = 0.0039; counting each of the 5 sources once, p = 0.0625) | results page, "Held-out tasks, re-run after the fork fix", exact-test table for the second held-out rule |
| Repair: 45% → 44%, 90% → 88% | results page, "The repair patch" |
| Second app (T14): 0 of 33 vs 10 of 32, p = 0.0078 | **PR #102 description; not yet on the results page** |
| Thinking-off savings, json_object vs json_schema, fork timings | `docs/FACTS.md` sections 2, 3 and 6 |
| 1,474 AI messages, 36 live runs, 0 tool calls as text | `docs/SPEC.md`, Changes, 2026-10-16 (Preflight) |
| More than 2,400 tests | `uv run pytest --collect-only -q` on `main` (re-count on the day) |

## Before you submit

- [ ] Put the YouTube link in the table above.
- [ ] Re-check every number against the results page you submit with. The page was republished on
      2026-10-21; the cost table, run accounting, replay page and the Tavily set came after.
- [ ] **Second app:** if the T14 results are not on the results page by then, delete that row and the
      "Second app" sentences. Do not leave a number the page does not show.
- [ ] Held-out wording: keep "exploratory". If you quote the pooled p = 0.0039, quote 0.0625 (each source
      counted once) next to it, and lead with the second.
- [ ] Open the demo URL and the repo's About box in a private window; the licence must be visible.
- [ ] Watch the video once with no context, as a judge would.
