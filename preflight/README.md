# preflight/

C0, the serving preflight (docs/SPEC.md): probes on the exact models and settings, the safe config
they imply, and an offline cost summary. A owns this folder.

## Run

```
uv run python -m preflight --out proposal.json                 # prints the call count, stops
NEMOGATE_LIVE=1 uv run python -m preflight --out proposal.json --yes [--tries N]
uv run python -m preflight.cost RUN_DIR...                     # offline, no model call
```

Without `NEMOGATE_LIVE=1` it only prints the plan. With it, it also needs `--yes`. **It spends
credit**: a default run (`--tries 3`) makes **36 model calls** (3 roles x 4 per try x 3) plus one
free `GET /v1/models`; `--tries N` makes 12 x N. Each call goes through `get_model(role)`, so the
spend cap applies.

It never writes `config.yaml` and refuses `--out` pointing at it. The proposal JSON holds the
derived safe config and its hash, the fields that differ from `config.yaml`, per-role pass/fail,
warnings, every probe result with failure evidence, and the configured model ids. Exit code 0 means
a safe config was found and nothing needs a human; 1 means a warning or no safe config. A changed
`config.yaml` needs Shekhar's approval (shared file); copy the proposal in by hand.

## Probes (`probes.py`)

| Probe | Per role | Passes when |
| --- | --- | --- |
| `escape` | `json_schema` and `json_object`, thinking off | every reply is a JSON object with the field. `counts["exact"]` is how many also copied the string (quotes, backslashes, newline) unchanged; a copying slip is a finding about the model (docs/FACTS.md section 6), so it does not fail the probe |
| `tool_call` | `json_schema`, thinking off | every reply has a native `tool_calls` entry for the offered tool. A call written into `content` is counted as `as_text` (`looks_like_tool_call_text`, the NAT #2274 class) |
| `thinking` | `json_schema`, thinking on | structured output is still valid |
| `model_ids` | once | the id in `config.yaml` is exactly among `GET /v1/models` (`common/tokenfactory.py`) |

Each variant model is built by `get_model(role)` from a temporary copy of the config with that
variant's safe config and hash (`run.variant_models`), so the guards in `common/llm.py` apply.

## Safe config (`safe_config.py`)

`json_schema` if it is valid for every role with thinking off, else `json_object`, else none.
Thinking stays off whenever a format works with it off; on only as a last resort, with a warning.
The hash is `common.llm.safe_config_hash` over the proposed settings and the config's sampling.

## Cost (`cost.py`)

Reads `record.json` from each RUN_DIR (a run folder, or a folder of run folders) and prints n, mean,
median, max and total USD per arm and over all runs. The "all" mean is what
`python -m replay.budget --from-runs` plans with.
