# preflight/

C0, the serving preflight (docs/SPEC.md): probes on the exact models and settings, the safe config
they imply, and an offline cost summary. A owns this folder.

## Run

```
uv run python -m preflight --out proposal.json                 # prints the call count, stops
NEMOGATE_LIVE=1 uv run python -m preflight --out proposal.json --yes [--tries N]
uv run python -m preflight.cost RUN_DIR...                     # offline, no model call
uv run python -m preflight.textcalls RUN_DIR_OR_PARENT... [--json OUT]   # offline, no model call
```

Without `NEMOGATE_LIVE=1` it only prints the plan. With it, it also needs `--yes`. **It spends
credit**: a default run (`--tries 3`) makes **54 model calls** (3 roles x 6 per try x 3) plus one
free `GET /v1/models`; `--tries N` makes 18 x N. Each call goes through `get_model(role)`, so the
spend cap applies.

It never writes `config.yaml`: `--out` is refused when it is the `--config` file, the repo's
`config.yaml`, or any file git tracks. The proposal JSON holds the
derived safe config and its hash, the fields that differ from `config.yaml`, per-role pass/fail,
warnings, every probe result with failure evidence, and the configured model ids. Exit code 0 means
a safe config was found and nothing needs a human; 1 means a warning or no safe config. A changed
`config.yaml` needs Shekhar's approval (shared file); copy the proposal in by hand.

## Probes (`probes.py`)

| Probe | Per role | Passes when |
| --- | --- | --- |
| `escape` | `json_schema` and `json_object` with thinking off; `json_schema` with it on | every reply is a JSON object with the field. `counts["exact"]` is how many also copied the string (quotes, backslashes, newline) unchanged; a copying slip is a finding about the model (docs/FACTS.md section 6), so it does not fail the probe |
| `tool_call` | `json_schema`, thinking off and on | every reply has a native `tool_calls` entry for the offered tool. A call written into `content` is counted as `as_text` (`looks_like_tool_call_text`, the NAT #2274 class) |
| `thinking` | `json_schema`, thinking on | structured output (an extraction) is still valid |
| `model_ids` | once | the id in `config.yaml` is exactly among `GET /v1/models` (`common/tokenfactory.py`) |

Each variant model is built by `get_model(role)` from a temporary copy of the config with that
variant's safe config and hash (`run.variant_models`), so the guards in `common/llm.py` apply.

## Safe config (`safe_config.py`)

`json_schema` if it is valid for every role with thinking off, else `json_object`, else none.
Thinking stays off whenever a format works with it off. On is a last resort, proposed only when
`thinking`, `escape` and `tool_call` all passed with thinking on for every role; otherwise there
is no safe config and the warning says which role and probe failed.
The hash is `common.llm.safe_config_hash` over the proposed settings and the config's sampling.

## Cost (`cost.py`)

Reads `record.json` from each RUN_DIR (a run folder, or a folder of run folders) and prints n, mean,
median, max and total USD per arm and over all runs. The "all" mean is what
`python -m replay.budget --from-runs` plans with.

## Text tool calls (`textcalls.py`)

Offline scan of finished runs for a tool call a model wrote as text instead of a native `tool_calls`
entry (the NAT #2274 class, so the framework never runs it). `trace.jsonl` does not hold ordinary AI
messages; `checkpoints.sqlite` does, for the planner (namespace `''`) and the subagents (`tools:<id>`)
alike (docs/FACTS.md section 5). For each AI message with no native tool call it runs
`looks_like_tool_call_text` and prints the run, agent, namespace, step, message number, the tool it
looks like and a 200-character excerpt. It also checks `final_output` and `boundary_in` texts in
`trace.jsonl`. The last lines count runs, AI messages and candidates.

PATHs may be run folders or parents of them (searched recursively; anything with `heldout` in its
path is skipped). A run folder is never written: the checkpoint file is copied to a temp folder first.
A fork's copied history is left out when its source folder is found. A hit is a candidate to read:
prose or quoted JSON that happens to look like a call is flagged too. `--json OUT` also saves every
result; OUT must be a new file outside every scanned folder and run folder.
