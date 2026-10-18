# report/

Two pages, both generated offline from run data. Neither calls a model.

## Results page (`site.py`)

```
uv run python -m report.site --results results/results.json --out results/site/index.html \
    [--runs DIR...] [--repair DIR] [--ledger nemogate.yaml]
```

Reads the `results.json` that `replay/stats.py` writes (`format_version` 2; any other version is
refused with exit code 2) and writes one self-contained HTML file: inline CSS and SVG, no script,
no font, CDN or other network request. The output depends only on the input, so two runs are
byte-identical. Generated pages are not committed.

What it shows:

- Top: one plain verdict line made from the data (the largest rule effect among graded
  constraints, preferring one whose interval excludes 0, with its runs, interval and what the
  gated runs did in the same case; when gated was never broken, the rule is one the gate blocks
  before the call (`blocked_before_call` in the results, from `gate.policy`) and some gated run
  was blocked, it adds that the 0 comes from blocking, which block-only gets by construction;
  for any other rule it adds nothing of the kind; when that handoff was run under more than one
  compaction setting, the sentence names the setting it quotes and says the others have their own
  rows below; "No comparison has enough runs yet" when there
  is none), then a key defining the four arms and the operators in method words.
- Under the key, "What each part fixes": four static lines. The gate blocks a tool call that
  breaks a rule checkable before the call (path, tool and approval rules), whatever the cause, and
  gated also holds back a reply that misses an output rule (`gate/policy.py`, `gate/middleware.py`);
  repair targets rules lost in the handoff (`repair/menu.py`); rules checked on the filesystem diff
  are graded after the run and not blocked (docs/SPEC.md, predicate table). A fourth line
  says that for a path, tool or approval rule a 0 broken in the block-only and gated arms comes
  from blocking (the gate refuses with the check that grades the run, so both get it by
  construction), that for other rules a 0 is a measured result, and that whether carrying the
  rule adds anything over blocking is the hypothesis being measured, not a result.
- Header: the reproducibility group (short hashes, with the full value in the tooltip, and the
  model ids), run accounting (found, pooled, inconclusive by reason) and the honesty strip
  (extraction recall or "not measured", constraints with a compiled check, inconclusive total).
  The strip is always shown, including when nothing was pooled.
- Boundary graph: agents are parsed from the `delegation:<from>-><to>` and
  `return:<from>-><to>` boundary ids found in the data; nothing is hard-coded. A
  `compaction:<agent>` boundary is a marker on its agent. Each edge is coloured by the pooled
  violation rate of the `mutated` cells at that boundary over all constraints and operators (the
  Wilson interval is recomputed from the summed counts). A boundary with no mutated runs falls
  back to its `natural` runs and the label says "natural". No cell of either arm shows "no data",
  a neutral dashed edge, never 0. Colour is the Okabe-Ito set (readable with colour-vision
  deficiency); the rate is also the line thickness and the text on the edge, and a table under the
  graph lists the same numbers. Each edge links to the card of the first constraint that has the
  boundary; the table links to all of them.
- One card per constraint: rule text, kind, a Graded / Not graded tag, and per boundary a
  table of cells (arm, operator, runs, violation rate and task success with their 95% Wilson
  intervals and a small interval bar). A `natural` cell whose results carry `violations_by_label`
  adds one line under the violation rate, such as "2 of 3 broken: 1 lost in the handoff,
  1 carried but disobeyed"; without the field (older results files) nothing is added. When the
  cell with operator `none` (natural, block_only, gated) carries `labels` (every run counted by
  carriage label) the card shows a small grid instead; under gated a line says "in the handoff"
  includes the gate's re-pin, and the older line and the "never came up" line are not repeated: rows "Rule in
  the handoff" (carried) and "Rule lost in the handoff" (dropped), columns "Agent kept it" and
  "Agent broke it", so `CARRIED_OBEYED`, `DISOBEYED`, `DROPPED_LUCKY` and `DROPPED` each fill one
  square. Under block_only and gated the second column reads "Agent broke it or tried to" and a
  line says blocked attempts count there while the violation rate counts only what got through
  (a blocked attempt is labelled `DISOBEYED`). Runs the classifier could not place (`NOT_EXERCISED`, `UNKNOWN`, `UNCLASSIFIABLE`, no
  carriage.json) are counted under it as "Not placed". Its CSS (`presence.css`) is added only to
  a page that has a grid, so a page without one is unchanged. A cell that was not run prints "not run", not 0. A cell with
  `clustered: true` carries a warning that its intervals are too narrow. The rule effect
  (`load_bearing`), gate effect on task success (`ledger_value`) and gate effect on attempts
  (`attempt_reduction`, shown only when the results file has it) are shown with their interval,
  and an interval that crosses 0 says "Not distinguishable from 0 at n = N." A constraint with
  `has_check: false` is tagged Not graded and shows no rate, comparison or trace (only a count of
  its runs); its cells are also left out of the boundary graph's pooled rates.
- Task success also shows the three-way split when a cell has `outcomes`: "done N · kept the rule,
  left open N · broke the rule N", and a foot note under the table says what "kept the rule, left
  open" means: the rule was obeyed but the task was not finished (for example a deletion that
  waited for the user), the cost of complying and not a failure to comply. A run that broke the
  rule counts only as broke, even if the task finished. The existing columns stay; results files
  without the field render to the same bytes as before.
- A cell with `arm_metrics` also shows "Attempted the rule's action" in the cell table, beside
  the violation rate and task success: the share of runs that made at least one call at the
  forbidden action, blocked or not (`arm_metrics.attempt_rate`, Wilson 95%). A foot note says
  why: the violation rate counts only what got through, so a blocking arm can show no violations
  while the coder still tried. A cell with `arm_metrics` but no rate says "no trace to count
  from" or "not about a tool or path", and a cell without `arm_metrics` says "not in this
  results file". Results files from before `arm_metrics` get no column and render to the bytes
  they always did.
- Under each cell table, "Cost and time per run": per arm and operator, mean cost and wall time
  with a 95% bootstrap interval from five runs and the lowest to highest value, labelled
  "range", from fewer (unknown time is said, not shown as 0; the means cover graded runs only,
  and the table says so), and for
  the block_only and gated arms what happened after a block (blocked calls and repeats per run,
  carry-over, re-plans, runs stuck, recovery steps with the cost and time shares marked as
  estimates; a line says how many runs carry-over and re-plans leave out because several task
  calls ran at once). A cell holding fork records (`n_forks` in the cell) says under its cost,
  time and recovery shares that they are "after the fork point only" ("all forks", or "k of n
  runs are forks"): a fork's cost and time cover only what ran after the fork point, so they are
  not whole-run numbers. A cell without `arm_metrics` (older file) shows "not in this results file", and a
  page with none prints no table. The header's run block adds "Money in no rate": what errored
  runs cost, in all and per arm.
- A one-sentence summary per (boundary, operator), only when the mutated violation rate, the
  block-only success and the gated success all exist. It uses those cells' numbers and nothing
  else.

Tests: `uv run pytest report/tests/test_site.py`. Fixtures in `report/tests/fixtures/`:
`results_runs.json` is generated from `run_folders/` and `ledger.yaml` by `replay.stats`
(`uv run python -m replay.stats report/tests/fixtures/run_folders --ledger
report/tests/fixtures/ledger.yaml --out report/tests/fixtures/results_runs.json`; a test
regenerates it and compares), so the numbers and the trace panel describe the same runs.
`results_rich.json` is hand-written, has no run folders behind it, and is for page features: (two constraints, a return edge, a clustered cell, a null
extraction recall, an interval crossing 0, missing cells, markup in a rule text) and
`results_empty.json` (nothing pooled, `reproducibility` null).

### Evidence panels (`panels.py`)

- **Coverage** (always): from the honesty block, the pooled constraints and, with `--ledger`, the
  full ledger. Declared, with a compiled check, exercised (at least one pooled run), exercised and
  checked, extraction recall ("not measured" when null), and the unchecked and never-exercised
  rules by id and text. Without `--ledger` only constraints that had pooled runs can be named.
- **Trace at the violating action** (`--runs`, run folders or folders of them): in each
  constraint's boundary section, the first violating `mutated` run by run id beside the first
  comparison run that kept the rule (a gated run with the same operator, else a natural run;
  same task, rule and boundary, and `is_comparable`). Only runs the statistics counted are
  used: those in the results file's `reproducibility` group that `replay.stats` does not set aside
  (`stats.is_exercised`; a folder it cannot read is skipped), so the trace comes from the runs
  the numbers are made of.
  Each side shows the planner's text diffed against what the receiver got (a line tagged
  "(dropped)" was sent but lost, "(added)" was received but not sent; "nothing was changed in
  transit" is said only when the full payloads are equal, not just the shown part) and the action
  named by `violations.json` (a fork's steps count `prefix.jsonl` first, and the page says so).
  A violation with no step reads "no single action: judged on the final files", except for an
  `output_must_include` rule, where it means the graded agent's reply never arrived (none sent,
  or the gate withheld every one) and the page says that.
  The comparison side says "No such call" when it never made the call. Trace text is escaped.
- **Repair patch** (`--repair`, the folder `python -m repair` writes): reads `repair-*.md` and the
  `.patch` beside it. Without `--repair` it says "No repair run yet". `repair` writes no
  structured result, so the numbers are parsed from the `Result:` line `write_report` writes. If the summary has more than one line starting `- Result:` (a model's
  rationale, or a file name quoted in a guard's reason, can forge one) the result is ambiguous:
  no numbers table, the raw text is shown escaped. The
  summary does not say which constraint, arm or operator the numbers came from; the page says so.

## Publishing on GitHub Pages

The demo URL is the results page served by GitHub Pages from the `gh-pages` branch, which holds
only the built page (`index.html`) and an empty `.nojekyll`. The page is built locally, because
its trace and patch panels read `runs/`, which is not in git. From the repo root:

```
uv run python -m replay.stats runs/r-* --ledger nemogate.yaml --out results/results.json
uv run python -m report.site --results results/results.json --out results/site/index.html \
    --runs runs --ledger nemogate.yaml [--repair runs/repair]
git fetch origin gh-pages && git worktree add /tmp/nemogate-pages gh-pages
cp results/site/index.html /tmp/nemogate-pages/index.html
git -C /tmp/nemogate-pages commit -am "Publish results page" && git -C /tmp/nemogate-pages push
git worktree remove /tmp/nemogate-pages
```

Pages serves it at `https://ritamgh.github.io/nemogate/` a minute or so after the push. Whatever
is on `gh-pages` is public, so publish only pages built from runs you mean to show. Serving on
Nebius Serverless Endpoints instead is optional (the hackathon encourages it but does not require
it, and its credits are Token Factory only).

## Deploying the results page on Nebius Serverless Endpoints (optional)

`serve.py` is a stdlib file server for `results/site/` (GET and HEAD only, no directory listing,
`/healthz` returns 200, port from `--port` or `$PORT`, default 8080). `Dockerfile` copies it and
the built page into a `python:3.12-slim` image that runs as a non-root user. The root
`.dockerignore` lets only those files into the build context, so `.env` never enters it. Local
check, no cloud: `uv run python -m report.serve --dir results/site` then open
`http://localhost:8080/`. The facts below are in `docs/FACTS.md` section 11; most are from the
docs and not yet confirmed by a real deploy.

Run from the repo root. Placeholders: `<region>` (the project's region, for example `eu-north1`),
`<subnet-id>` (only if the project has more than one subnet), `<tag>` (for example the short
commit hash).

```
# 1. Build the page (needs results/results.json; add --runs/--repair/--ledger as for the full page)
uv run python -m report.site --results results/results.json --out results/site/index.html

# 2. One-time: create a registry and let Docker authenticate to it (do not run docker as root)
export REGION_ID=<region>
export REGISTRY_PATH=$(nebius registry create --name nemogate-report --format json \
    | jq -r ".metadata.id" | cut -d- -f 2)
nebius registry configure-helper

# 3. Build and push
IMAGE=cr.$REGION_ID.nebius.cloud/$REGISTRY_PATH/nemogate-report:<tag>
docker build --platform linux/amd64 -f report/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"

# 4. Create the endpoint (CPU only, smallest preset, 20 GiB disk, no token: the page is public)
nebius ai endpoint create --name nemogate-report --image "$IMAGE" --container-port 8080 \
    --platform cpu-d3 --preset 2vcpu-8gb --disk-size 20Gi [--subnet-id <subnet-id>]

# 5. Wait for it to be running (about five minutes), then read the public URL
nebius ai endpoint list
nebius ai endpoint get <endpoint-id> --format json \
    | jq -r '.status.public_endpoints[] | select(startswith("https://"))' | head -1
curl -fsS <url>/healthz        # ok

# Update the page: rebuild step 1, push a new <tag>, then delete the endpoint and repeat step 4
# (an in-place image change was not read in the docs).
# Stop billing: stop keeps the endpoint without compute charges; delete removes it.
nebius ai endpoint stop --id <endpoint-id>
nebius ai endpoint delete --id <endpoint-id>
```

If no `https://` URL shows up in `status.public_endpoints`, add `--public` to step 4 (the
quickstart's example uses it; the docs say it is not required). The page holds run data and is
public with this command; add `--auth token` for a private endpoint, then every request needs an
`Authorization: Bearer <token>` header, which a browser visit does not send.

Cost: a running endpoint bills Compute VM and disk prices until it is stopped or deleted; there is
no scale-to-zero in the docs. The price page lists the AMD EPYC Genoa class "from $0.13" per hour
(4 vCPUs and up), so a 2 vCPU endpoint is at or below about $3 a day; this is not confirmed for
the preset. The registry stores the image and may bill for it. Stop the endpoint after the demo.

Tests: `uv run pytest report/tests/test_serve.py` (real sockets on 127.0.0.1, no cloud).

## Natural runs page (`natural_runs.py`)

`uv run python -m report.natural_runs --out results/natural-runs.md RUN_DIR_OR_PARENT...` writes a
markdown page of every natural run with its carriage label. See the module docstring.
In the all-runs table, "Task done" shows `open` (not a cross) for a run with no violation whose
task was not finished: the rule was kept and the job waited, which is the cost of complying, not a
failure. A legend line above the table says so. A cross still means not finished with a violation
or an unknown verdict.
