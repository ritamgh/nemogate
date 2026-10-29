# Feedback on Nebius Token Factory, Nebius Sandboxes and the NVIDIA Nemotron models

From the NemoGate team (Ritam Ghosh, Shekhar Padhy). We ran several hundred live agent runs through Token
Factory and Sandboxes over about three weeks, so this is feedback from sustained use, not a first impression.
Every item below says what happened, where the evidence is, why it matters to a builder and what
would fix it. The evidence is in [`docs/FACTS.md`](../FACTS.md) (each row has a status and a source),
the run folders it names, and the tests in this repository.

**What we used:** Token Factory with three models (`nvidia/nemotron-3-super-120b-a12b`,
`nvidia/Nemotron-3_5-Lightning`, `nvidia/Nemotron-3-Ultra-550b-a55b`); Nebius Sandboxes through the
`contree-sdk` and its Deep Agents backend; Tavily for web search in a second app. **What we did not
use:** NeMo Guardrails and the NeMo Agent Toolkit. We read their documentation to position our work
(see the README) and have no hands-on feedback on them.

## The six changes we would ask for first

| # | Ask | Product | Why first |
| --- | --- | --- | --- |
| 1 | Say, on every model page and in the API reference, that thinking is on by default and billed as output tokens, show how to turn it off, and report reasoning tokens in `usage` | Token Factory, Nemotron | It silently multiplied our output tokens 3 to 16 times |
| 2 | An image delete call and a lifetime for images, plus a way to count your own images | Sandboxes | Every command makes an image and there is no way to remove or count them |
| 3 | A recoverable session after a timeout or a cancelled call, or a documented recovery pattern | Sandboxes | One timeout makes every later call on that session fail |
| 4 | Fail fast when credentials are missing, and say what the default working directory is | Sandboxes SDK | A missing variable gave a confusing 401 or 403; a path-less `grep` took 41.5 s |
| 5 | A table of which `response_format` values each model supports, and an error instead of prose when one is not | Token Factory | `json_object` on Super returned reasoning text, not JSON |
| 6 | Document the fork pattern (new session from a snapshot) as the supported way to branch a run | Sandboxes | It is the feature that made our whole method possible, and it took reading the SDK to find |

---

## Nebius Token Factory

### What worked well

- **One OpenAI-compatible endpoint for three model sizes.** One key and one base URL served a 30B
  (3B active) helper, a 120B (12B active) agent model and a 550B (55B active) planner, so choosing a
  model per job was a config change (`common/llm.py`, `config.yaml`).
- **`GET /v1/models` returns price, context window and rate limits per model.** We built the budget
  check from it (Lightning $0.06 / $0.24, Super $0.30 / $0.90, Ultra $1.00 / $3.00 per 1M tokens in / out).
- **Structured output and tool calls were reliable.** `json_schema` gave valid JSON in all 24 of our
  escape-heavy probes across four models, with thinking on and off. Tool calls came back as native
  `tool_calls`; a scan of saved runs found none written as text in 1,474 AI messages over 36 live runs.
- **The price made experiments affordable.** A live run cost about $0.12 to $0.19 for a full
  three-agent coding task.

### 1. Thinking is on by default, billed as output, and hard to see

- **What happened.** Thinking is on by default on all four models we tried. `extra_body={"chat_template_kwargs":
  {"enable_thinking": False}}` turns it off; a `/no_think` line in the system prompt does not. The
  reasoning text is not returned through `langchain-openai` (it drops `reasoning_content`), but the
  tokens are billed as output.
- **Evidence.** Turning it off cut output tokens 3 to 16 times and latency 2 to 10 times, with the same
  validity and tool-call results (`docs/FACTS.md` section 6, "Safe config chosen"). On one helper
  prompt Lightning used about four times Nano's output tokens by default (mean 2,085 against 519).
- **Why it matters.** A builder sees a bill and a latency they cannot explain from the visible output.
- **Suggestion.** State the default and the switch on each model's page. Put reasoning tokens in
  `usage.completion_tokens_details`. Consider making `enable_thinking` a documented top-level parameter.

### 2. `response_format` support differs by model, and failure is prose

- **What happened.** Super answered `json_object` with reasoning-style text (0 of 3 valid JSON) and
  obeyed `json_schema` (3 of 3). Lightning and Nano handled both.
- **Why it matters.** The request succeeds, so the failure shows up as a parse error far downstream.
- **Suggestion.** Publish the supported values per model, and return an error for an unsupported one.

### 3. `seed` is accepted and not honoured

- **Evidence.** Super at temperature 0.8 with `seed=7`, sent twice, gave different text (FACTS section 2).
- **Suggestion.** Document it, or honour it. Reproducible research on the platform needs one of the two.

### 4. Smaller things

- **Model ids are cased inconsistently** (`...Nano-30B-A3B`, `nemotron-3-super-...`, `...Ultra-550b-...`).
  Our spec and a test fixture used all-lowercase ids and were wrong. Read ids from `/v1/models`, or normalise them.
- **At temperature 1.0 and top-p 0.95** (the model card's recommendation), Super kept JSON valid but
  copied escape-heavy text exactly in only 5 of 10 tries across two probes. If exact copying matters
  (we hit it writing code patches, below), the card could say so.

---

## Nebius Sandboxes (Contree)

### What worked well

- **Forking is cheap.** A new session on a snapshot, plus one command, took a median of 1.26 s (range
  0.65 to 1.86 s) over 10 sequential forks; 10 forks at once took 6.3 s with no rate-limit retries.
  Every one of our runs, and every fork of a run, executes this way.
- **The Deep Agents backend works.** `ContreeSandbox` plugged into a Deep Agents team with its file
  tools running inside the sandbox, and the filesystem diff between two images gave us an exact
  record of what the agents changed.
- **Limits are visible** through the token info (`instance_max_timeout` 3600 s,
  `instance_max_concurrency` 50, `instance_max_layer_bytes` 12 GiB).

### 1. There is no fork call, so the central pattern has to be discovered

- **What happened.** There is no "fork", "snapshot" or "rollback" method. A fork is a new session on
  the snapshot's image uuid, and the session's uuid is replaced in place after every run.
- **Evidence.** FACTS section 3, "How to fork from an image" and "How to roll back to an image"
  (source references to the SDK files).
- **Suggestion.** Document this as the supported branching pattern with an example, and expose the
  current image uuid on the Deep Agents backend (`ContreeSandbox` has no public accessor; we keep our
  own reference to the session).

### 2. Every command makes an image, and images cannot be deleted or counted

- **What happened.** `execute` runs with `disposable=False`, so each command creates an image; a file
  write costs two (a preflight execute plus the upload). We expect hundreds of images per agent run.
  The SDK has no delete call. Listing images returns about 39,600 items, almost all public imports,
  and took about five minutes, so we could not measure our own count or retention.
- **Why it matters.** We could not tell whether a large sweep was approaching a quota or leaving
  storage behind.
- **Suggestion.** An image delete (or a time-to-live), and a usage endpoint with your own image count and bytes.

### 3. One timeout or cancellation ends the session

- **What happened.** `aexecute("sleep 30", timeout=3)` raises after about 3.8 s and the session goes
  to `FAILED`; every later command raises `ContreeImageStateError`. A cancelled call left a session in
  `EXECUTING` with no way out.
- **A real case.** Deep Agents' `grep` wraps the command in `asyncio.wait_for` (35 s). When it timed
  out, the cancel landed inside the session and the agent's next tool call raised, which ended a whole
  run. We wrote a wrapper that runs each command as a shielded task (`replay/sandbox.py`,
  `replay/tests/test_sandbox.py`).
- **Suggestion.** Let a failed session be reset to its last good image, or document the rebuild pattern
  next to the timeout parameter.

### 4. Credentials and the working directory

- **Missing credentials fail late and confusingly.** If `NEBIUS_API_KEY` or `NEBIUS_PROJECT_ID` is
  unset, the client still builds and sends the variable *name* as the token. A stale
  `~/.config/contree/auth.ini` can also fill them silently. Suggest raising at construction.
- **The default working directory is `/`.** `ContreeSandbox` passes no `cwd`, so a path-less `grep`
  scanned the whole filesystem: 41.5 s and a timeout, against 0.9 s with `cwd` set. Five of five
  path-less greps timed out in earlier live runs. Suggest a `cwd` argument on the Deep Agents backend
  and a line in its docs.

### 5. Smaller things

- **A 403 on every call.** About an hour after two image builds succeeded, every call from one
  account returned 403 Forbidden, including `get_token_info()`. We recorded it once and do not know the cause.
- **The cost unit.** `result.cost` was about 0.00006 to 0.00008 per trivial run; we could not find its unit.
- **No CPU, memory or network settings** in the SDK; the spawn request carries none.
- **Output is decoded as strict UTF-8,** so a command that prints other bytes raises *after* the run
  has already advanced the session's image.

---

## NVIDIA Nemotron models

### What worked well

- **Ultra picked the fix that matched the diagnosis.** In both live repair rounds it chose, from a
  closed menu, to carry the rule across the handoff. (The weak link was the patch Super then wrote,
  below, and then prompt-only repair not holding; that is a result about the fix, not about the plan.)
- **Super was a reliable tool user.** Native tool calls every time we looked, across a three-agent
  team over hundreds of runs.
- **The size split fits the work.** Lightning as a cheap helper, Super as the agent team, Ultra for rare planning.

### Observations that cost us time

1. **Lightning ignores an "edit this text" instruction in a way that looks like success.** Asked to
   remove a rule from a delegation, it returned the delegation unchanged in 3 of 3 probes; asked to
   quote the spans to cut, it over-selected and erased whole messages. It was exact (12 of 12) when
   asked only for the *numbers* of the sentences, with the code doing the edit. On one later live run
   it picked every sentence. Lesson for builders: use it to classify, not to rewrite.
2. **Super wrote a repair patch with an anchor that did not exist.** In the first live repair round
   its edit quoted a line with the wrong quotes and backslash (0 occurrences), and our guard refused
   it twice. The second round inserted text after a backslash line-continuation, so the patched prompt
   read "...for the user.Copy every rule...". Our fixes: show the model the real lines on retry, and
   reject an insert that runs into its neighbour (`repair/guards.py`). Exact string copying is where
   we saw it fail.
3. **Tool-call-as-text did not occur.** We were looking for the failure class behind NeMo Agent Toolkit
   issue 2274 and found none (0 in 1,474 messages). We report it because it is a result: the failure
   did not appear on this stack.

---

## Other libraries we depend on

- **Structured-output errors have four shapes.** With `langchain-openai` and `json_schema`, a bad
  reply raised `pydantic.ValidationError`, `LengthFinishReasonError`, `OpenAIRefusalError` or a plain
  `ValueError` ("does not have a 'parsed' field") depending on the failure. A retry has to know all four
  (`gate/operators.py`, with tests built from each library's own parser).
- **Deep Agents.** Subagents are isolated by default (they see only the delegated description), and
  an experimental `fork` mode shares the parent's conversation. Our whole study is about what the
  isolated default loses; we have not measured `fork`. Separately, `create_deep_agent` adds a
  `general-purpose` subagent with every tool, which would let a planner delegate around the
  restrictions we set (we disable it), and on a sandbox backend `execute` and `delete` reach every
  agent unless each agent's filesystem tools are restricted.

## Thank you

The combination of cheap open-weight models and fast snapshot forks is what made this project
possible: we could re-run a task from the same point under four conditions, hundreds of times, for
about $38 of the $60 credit on one account (the console balance read $22.20 on Oct 28, after the repair rounds, the sealed final sweep and the research-app runs). The asks above are the places where the platform made that harder than it needed to be.
