# report/

Two pages, both generated offline from run data. Neither calls a model.

## Results page (`site.py`)

```
uv run python -m report.site --results results/results.json --out results/site/index.html
```

Reads the `results.json` that `replay/stats.py` writes (`format_version` 1; any other version is
refused with exit code 2) and writes one self-contained HTML file: inline CSS and SVG, no script,
no font, CDN or other network request. The output depends only on the input, so two runs are
byte-identical. Generated pages are not committed.

What it shows:

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
- One card per constraint: rule text, kind, whether it has a compiled check, and per boundary a
  table of cells (arm, operator, runs, violation rate and task success with their 95% Wilson
  intervals and a small interval bar). A cell that was not run prints "not run", not 0. A cell with
  `clustered: true` carries a warning that its intervals are too narrow. The load-bearing score and
  ledger value are shown with their interval, and an interval that crosses 0 says "Not
  distinguishable from 0 at these sample sizes."
- A one-sentence summary per (boundary, operator), only when the mutated violation rate, the
  block-only success and the gated success all exist. It uses those cells' numbers and nothing
  else.

Tests: `uv run pytest report/tests/test_site.py`. The fixtures in `report/tests/fixtures/` are
hand-written: `results_rich.json` (two constraints, a return edge, a clustered cell, a null
extraction recall, an interval crossing 0, missing cells, markup in a rule text) and
`results_empty.json` (nothing pooled, `reproducibility` null).

## Natural runs page (`natural_runs.py`)

`uv run python -m report.natural_runs --out results/natural-runs.md RUN_DIR_OR_PARENT...` writes a
markdown page of every natural run with its carriage label. See the module docstring.
