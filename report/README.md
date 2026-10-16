# report/

Two pages, both generated offline from run data. Neither calls a model.

## Results page (`site.py`)

```
uv run python -m report.site --results results/results.json --out results/site/index.html \
    [--runs DIR...] [--repair DIR] [--ledger nemogate.yaml]
```

Reads the `results.json` that `replay/stats.py` writes (`format_version` 1; any other version is
refused with exit code 2) and writes one self-contained HTML file: inline CSS and SVG, no script,
no font, CDN or other network request. The output depends only on the input, so two runs are
byte-identical. Generated pages are not committed.

What it shows:

- Top: one plain verdict line made from the data (the largest rule effect among graded
  constraints, preferring one whose interval excludes 0, with its runs, interval and what the
  gated runs did in the same case; "No comparison has enough runs yet" when there is none), then
  a key defining the four arms and the operators in method words.
- Under the key, "What each part fixes": three static lines. The gate blocks a tool call that
  breaks a rule checkable before the call (path, tool and approval rules), whatever the cause, and
  gated also holds back a reply that misses an output rule (`gate/policy.py`, `gate/middleware.py`);
  repair targets rules lost in the handoff (`repair/menu.py`); rules checked on the filesystem diff
  are graded after the run and not blocked (docs/SPEC.md, predicate table).
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
  adds one line under the violation rate, such as "2 of 3 broken: 1 rule dropped in the handoff,
  1 rule carried but disobeyed"; without the field (older results files) nothing is added. A cell that was not run prints "not run", not 0. A cell with
  `clustered: true` carries a warning that its intervals are too narrow. The rule effect
  (`load_bearing`) and gate effect on task success (`ledger_value`) are shown with their interval,
  and an interval that crosses 0 says "Not distinguishable from 0 at n = N." A constraint with
  `has_check: false` is tagged Not graded and shows no rate, comparison or trace (only a count of
  its runs); its cells are also left out of the boundary graph's pooled rates.
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

## Natural runs page (`natural_runs.py`)

`uv run python -m report.natural_runs --out results/natural-runs.md RUN_DIR_OR_PARENT...` writes a
markdown page of every natural run with its carriage label. See the module docstring.
