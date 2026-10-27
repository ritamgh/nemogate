# report/

Two pages, both generated offline from run data. Neither calls a model.

## Results page (`site.py`)

```
uv run python -m report.site --results results/results.json --out results/site/index.html \
    [--runs DIR...] [--repair DIR] [--recall FILE...] [--ledger nemogate.yaml] [--notes FILE] \
    [--appendix TITLE RESULTS_FILE [NOTES_FILE]]... [--appendix-open TITLE RESULTS_FILE [NOTES_FILE]]...
```

Reads the `results.json` that `replay/stats.py` writes (`format_version` 2; any other version is
refused with exit code 2) and writes one self-contained HTML file: inline CSS and SVG, no script,
no font, CDN or other network request. The output depends only on the input, so two runs are
byte-identical. Generated pages are not committed.

What it shows:

- Top: the title, one line on what was tested, and "How to read this" (four lines: the three
  outcomes, that "task passed" alone ignores the rule, what the arms do, where the exact numbers
  and the definitions are). The first line also says that the gate stops the rule from being
  broken, and that finishing the task after a block needs a route that keeps the rule or a human
  who can approve, so an unfinished task with the rule kept is not a gate failure.
- "At a glance", above "How to read this", only when the results set has exact tests (a results
  file without `exact_tests` renders byte for byte as before): one table of every graded rule,
  from the results file only. One row per rule and condition, because the arms of one rule do
  not always share their operators (the ungated arm is natural at "no sabotage" and mutated at
  an operator), so nothing is added up across rows: sabotage setting, plus handoff and
  compaction setting where the rule has several. Only conditions with a block-only or gated
  cell get a row; a graded rule with none still gets one row of dashes. Columns: "Broke the
  rule, no gate", "block-only", "gated" (k of n and the share, the cells the detail tables use;
  a dash for a missing cell, never 0) and "Exact test p (gated vs block-only)" (the `violations`
  entry of that operator and compaction setting, gated against block-only; a paired test also
  gives the figure that counts each source once; a dash when there is none, and a dash when both
  arms broke the rule 0 times, because block-only gets 0 by construction for an action it
  refuses, so 0 against 0 is not tested). When the set has `attempts` entries, two more columns:
  "Tried the forbidden action, block-only -> gated" (k of n and the share of each arm, from the
  cells' `arm_metrics.attempt_rate`, the detail table's figures; a dash for a cell without) and
  its exact test p (the `attempts` entry of the same operator and compaction setting, same
  rules). Without attempts entries the columns are not there. Each row links to
  its boundary section. The caption says the rows are all graded rules, not a selection. The
  table is repeated at the top of each appendix, from its own file (the rule's wording withheld
  there as everywhere in an appendix). It reuses the outcome table's CSS (`table.outcome`).
- "Cost of prevention" (`cost.py`, rendered by `render_cost` in `site.py`), directly under "At a
  glance" and for each appendix likewise, under the same condition (the set has exact tests) and
  only where cells carry `arm_metrics.cost`. One row per rule, sabotage setting and arm (gated,
  then block-only), against the arm at the same setting that has no gate (natural at "none",
  mutated at an operator). Columns: the broke-the-rule counts of the two arms with their 95%
  intervals; violations prevented per 100 runs; extra cost per run (the arm's mean cost minus the
  ungated arm's); cost per prevented violation = extra cost per run over prevented per run; and
  what the cost covers. Rules: (1) *like with like*: the arms are priced only when both are whole
  runs or both are all forks (`n_forks` equal to `n`). A fork's cost covers only what ran after
  the fork point, so a natural source against forks (the case at "no sabotage" for paired
  rules) gets no row; the page says "N comparisons left out", and a cell that mixes forks and
  whole runs is left out too. The last column says "Whole runs" or "After the fork point only".
  (2) A price needs more than 0 prevented and a positive extra cost. A gate that cost less is "<arm>
  cost less per run", never a negative price; nothing prevented, or more violations with the
  gate, or no extra cost, is a dash with its reason. (3) "Widest reading" under a figure: the ends
  of the two intervals the file holds (Wilson for the rates, bootstrap for the costs) combined the
  worst and the best way; the price's cheapest end is the least extra cost over the most
  prevented, the dearest the most extra cost over the fewest prevented. It is wider than an
  interval made for the price, and an end the data cannot fix reads "may cost less" or "no upper
  limit". A cost on the `range` basis has no interval, so the row shows the point values only and
  says "cost is a range, not a 95% interval"; a cell of fewer than five runs says so. Sharing of
  sources cannot be read from the file for the ungated arm, so two all-fork cells are priced
  together without that check. The caption says it is a price for this reference app and these
  rules, not a price list, that rates and prices come from the same runs, and to read the detail
  below. It never says "significant". No CSS: it reuses `table.outcome`. Tests:
  `report/tests/test_cost.py` (the arithmetic), `report/tests/test_site_cost.py` (the page).
- "What happened, rule by rule" (the first result on the page): one sentence that task success
  and keeping the rule are counted separately, with an example from the data (the cell with the
  most runs that broke the rule and still passed the task, `successes - outcomes.done`; when no
  cell has one it says so, and a file without `outcomes` says it cannot tell), a key to the bar,
  then per graded rule (tasks, rule id, the repo commit its runs were made at, a link to its
  card) a table with one row per cell: runs, "Did the job without breaking the rule"
  (`outcomes.done`, the headline, with a three-part bar: done solid, "Rule kept, task not
  finished" dotted, broke the rule striped and never in the done colour), "Rule kept, task not
  finished", broke, of those how many still passed the task, and "Task passed (rule not
  considered)". Under the bar, "Passed the task but broke the rule: K of N runs" shows when
  `successes - outcomes.done` is above 0 (nothing without `outcomes`). Under the "Rule kept, task
  not finished" count, "X of K were stopped by a block" shows for an arm whose cell has block
  counts (`arm_metrics.blocks.runs_with_block` above 0 and `stuck`): `stuck` counts runs with a
  block that failed the task, and a run that broke the rule can be among them, so with rule
  breakers in the cell it reads "X to Y of K"; no line when the data shows no such run. Where the rule
  has block-only and gated cells with block counts, "When the gate blocked a call" puts the two
  arms side by side: runs with a block, blocked calls and retries per run (95% bootstrap, or the
  range from fewer than five), runs stuck after a block, and the attempt rate, with the
  gated-minus-block-only attempt difference under it; the first such table on the page defines
  its words. Ungraded rules are named, not tabled. Then the verdict line: the largest rule effect
  among graded constraints, preferring one whose interval excludes 0, with its runs, interval and
  what the gated runs did in the same case; when gated was never broken, the rule is one the gate
  blocks before the call (`blocked_before_call` in the results, from `gate.policy`) and some gated
  run was blocked, it adds that the 0 comes from blocking, which block-only gets by construction;
  for any other rule it adds nothing of the kind; when that handoff was run under more than one
  compaction setting, the sentence names the setting it quotes and says the others have their own
  rows below; "No comparison has enough runs yet" when there is none.
- Exact tests (p-values), when a boundary of the results file has `exact_tests` (written by
  `replay/stats.py`; see `replay/README.md`, "Exact tests"). Under the gated-minus-block-only
  attempt lines of each boundary (main set and appendices alike) there is one table, a row per
  entry, built from the entries only. Columns: Outcome ("Broke the rule", "Tried the forbidden
  action", "Did the job and kept the rule"), Operator ("Pooled over operators" for the pooled
  entry, "no sabotage" for `none`, the compaction setting named under it when the boundary has
  several), Gated, Block-only, Test ("Fisher, independent runs" or "McNemar, matched forks of the
  same sources"), "p over runs or pairs" and "p counting each source once" (a dash for Fisher).
  Fisher shows each arm's `k of n runs`; McNemar shows, per arm, the matched pairs where only that
  arm had the outcome ("1 of 8 pairs (only this arm)"). The Gated and Block-only columns take the
  entry's `a`/`b` by their `arm` (so a swapped entry still lines up); an entry of other arms keeps
  its order and names each arm in its cell. Rows follow the order of the outcomes, then the
  compaction setting, then the operators, the pooled entry last under its outcome. p has three
  significant figures, "< 0.001" below that. Under the table one line gives the smallest p each
  source count can reach, `2 * 0.5^S` (at most 1), for the paired rows whose source test dropped
  no ties. The "How to read this" box gains one item (pairs, sources, the cautious source-level
  figure, no correction for many comparisons) only when some table is shown; the page never says
  "significant". A results file without `exact_tests` renders byte for byte as before.
- With `--notes FILE`, a section "Notes on these numbers" right after the results section (so
  after its verdict line, before the key): the file's paragraphs, split on blank lines, in order.
  It is for a re-grade or a caveat that is not in the results file; the text stays in the file,
  never in code. It is plain text and escaped, so markup shows as typed. Without the option, or
  with an empty file, nothing is added and the page is byte-identical to one built without it. A
  missing or unreadable file is exit code 2 and no page is written. A note may only repeat a
  number the page shows: each number in a notes file (a digit run with an optional decimal part,
  compared as typed; a run right after a letter, or a letter and a hyphen, is part of an
  identifier such as a task or rule id and is not a number) must be in the visible text of its own set, the page built with no notes at
  all (main part for `--notes`, the appendix's own section for its file). Otherwise exit code 2,
  one line per file naming it and, per line, the unmatched numbers, never the line's text.
- With `--appendix TITLE RESULTS_FILE [NOTES_FILE]` (repeatable), a separately labelled result
  set after everything else on the page, for runs that must be reported but not pooled with the
  main set. Each is drawn from its own results file only: its title, a line "Counts are small;
  read the intervals", the optional notes (plain text, escaped, like `--notes`), a run-accounting
  block (found, counted, left out by reason, models, short hashes), "Run accounting" (tables from
  the results file: found, counted and not counted for the set, and the counted runs per rule,
  sabotage setting and arm; a figure the file lacks is a dash, never 0; no per-rule errored
  count, the file holds counted runs only per rule), "How these runs differ from
  the main set" (changed hashes and model ids, mutation no-op counts; from the two results files),
  then per rule the "What happened" table, the block-only against gated table where it applies and
  the cell tables with their intervals. It never feeds the main page: the main part is
  byte-identical with and without it. The rule's wording is withheld ("Rule text withheld
  (held-out task)"; the card keeps the id, kind and counts), the task ids are not shown, and no
  run folder or trace is read (`--runs` does not apply). Ids are prefixed by the appendix number.
  A missing, unreadable or wrong-version file is exit code 2. Without the option the page is
  byte-identical to one built before it existed. Its CSS (`appendix.css`) is added only to a page
  that has an appendix.
- With `--appendix-open TITLE RESULTS_FILE [NOTES_FILE]` (repeatable), the same separately
  labelled set for a task that is **not sealed** (`Appendix.sealed` is False): the rule's wording
  replaces "Rule text withheld" in its card, "At a glance" and "Cost of prevention", the task ids
  ("task T14") show next to the rule id, and a line says "Not a sealed task". Nothing else
  changes: the set is still drawn from its own file, never feeds the main numbers, reads no run
  folder, and the main part is byte-identical with and without it. `--appendix` and
  `--appendix-open` share one list, so sets are numbered in the order given; the notes number
  check holds an open set's notes to its own section too. Use `--appendix` for anything sealed.
  Tests: `report/tests/test_site_appendix_open.py`.
- Every appendix, open or sealed, with `runs.errored_cost` in its file, gets "Errored runs, by arm"
  under Run accounting: per arm the errored runs (from `errored_cost.by_arm`; `replay.stats` lists
  only the arms that had one, so an arm not listed reads 0) and the runs counted in the rates (the
  cells' runs; a dash for an arm with no cell), plus an "All arms" row (`errored_cost.count` and
  the cells' total), and a line saying an errored run ended in an error before it could be graded
  and is in no rate. A file without `errored_cost` gets no table, not zeros.
- Below the results: the key (the four arms, the operators, and "Words on this page": boundary,
  compaction, not comparable, inconclusive and 95% interval in plain words; the statistical
  method stays in the Method block at the foot), then "What each part fixes": four static lines.
  The gate blocks a tool call that breaks a rule checkable before the call (path, tool and
  approval rules), whatever the cause, and gated also holds back a reply that misses an output
  rule (`gate/policy.py`, `gate/middleware.py`); repair targets rules lost in the handoff
  (`repair/menu.py`); rules checked on the filesystem diff are graded after the run and not
  blocked (docs/SPEC.md, predicate table). A fourth line says that for a path, tool or approval
  rule a 0 broken in the block-only and gated arms comes from blocking (the gate refuses with the
  check that grades the run, so both get it by construction), that for other rules a 0 is a
  measured result, and that whether carrying the rule adds anything over blocking is the
  hypothesis being measured, not a result.
- Header: the reproducibility group (short hashes, with the full value in the tooltip, and the
  model ids; with rules pooled at different commits it is the largest group, and the note beside
  it says each rule's own group is in its section, lists every commit with its rules, and labels
  the commit "largest group"), run accounting (found, pooled, inconclusive by reason) and the honesty strip
  (extraction recall or "not measured", constraints with a compiled check, inconclusive total).
  The strip is always shown, including when nothing was pooled. With `--recall FILE...` (the
  `ledger.recall` output, e.g. `results/recall/dev.json reference.json`) and no recall in
  `results.json`, the recall entry is one per file: "matched of total", the percentage, the extra
  checks proposed, labelled with the file's name, plus the note that this is not a clean held-out
  measure (the extraction prompts were tuned on the development set, FACTS section 6). A recall
  already in `results.json` is kept and the files are ignored. A file that is not a recall
  measurement (not JSON, a missing `matched`/`total`/`extra`, a fraction or negative count, more
  found than asked, none asked) exits 2.
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
- One card per constraint: rule text, kind, a Graded / Not graded tag, the tasks that
  contributed runs, the repo commit its runs were pooled at and how many of its runs were set
  aside as not comparable (rules are pooled separately, so they can differ), and per boundary a
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
  carriage.json) are counted under it as "Not placed". When none of the four squares has a run (for example a rule whose text is withheld, or runs with no carriage record), the grid and its notes are left out and one line says "Where the rule went in the handoff is not recorded for these runs." Its CSS (`presence.css`) is added only to
  a page that has a grid, so a page without one is unchanged. A cell that was not run prints "not run", not 0. A cell with
  `clustered: true` carries a warning that its intervals are too narrow. The rule effect
  (`load_bearing`), gate effect on task success (`ledger_value`) and gate effect on attempts
  (`attempt_reduction`, shown only when the results file has it) are shown with their interval,
  and an interval that crosses 0 says "Not distinguishable from 0 at n = N." A constraint with
  `has_check: false` is tagged Not graded and shows no rate, comparison or trace (only a count of
  its runs); its cells are also left out of the boundary graph's pooled rates.
- In the cards the task success column is headed "Task passed (rule not considered)", the gate's
  success comparison is "Gate effect on passing the task" (when both cells have `outcomes` it adds
  whether either arm broke the rule: if neither did, it is also the difference in clean
  successes), and the summary sentence says "task success (rule not considered)".
- Task success also shows the three-way split when a cell has `outcomes`: "done N · rule kept,
  task not finished N · broke the rule N", and a foot note under the table says what "Rule kept,
  task not finished" means: the rule was obeyed but the task was not finished (for example a deletion that
  waited for the user), the cost of complying and not a failure to comply. A run that broke the
  rule counts only as broke, even if the task finished. The existing columns stay; a card of a
  results file without the field is the card it was before (the outcome table at the top then
  says "not in this results file").
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

The second app's results (research team, Tavily; PRs #101 and #102) are shown this way, T14 and
the T13 pilot as two separate open sets, never pooled with each other or with the main set. Their
results files and notes are committed (`results/research/t14.json`, `t13.json`,
`results/notes/research-t14.txt`, `research-t13.txt`); the run folders are not in git. To rebuild
them, run `replay.stats` on one task's folders at a time (`replay.stats` pools by rule and T13 and
T14 share C-011), with the research app selected, then build:

```
NEMOGATE_APP=refapp.research.app:APP uv run python -m replay.stats RUNS/research-t14-a RUNS/research-t14-b \
    --ledger nemogate.yaml --out results/research/t14.json
NEMOGATE_APP=refapp.research.app:APP uv run python -m replay.stats RUNS/research-arms RUNS/research-pilot \
    RUNS/research-pilot-drop --ledger nemogate.yaml --out results/research/t13.json
uv run python -m report.site --results results/results.json --out results/site/index.html \
    --appendix-open "Second app: research team with web search (Tavily), task T14" \
    results/research/t14.json results/notes/research-t14.txt \
    --appendix-open "T13 pilot of the same app (not pooled with T14)" \
    results/research/t13.json results/notes/research-t13.txt
```

Tests: `uv run pytest report/tests/test_site.py`. Fixtures in `report/tests/fixtures/`:
`results_runs.json` is generated from `run_folders/` and `ledger.yaml` by `replay.stats`
(`uv run python -m replay.stats report/tests/fixtures/run_folders --ledger
report/tests/fixtures/ledger.yaml --out report/tests/fixtures/results_runs.json`; a test
regenerates it and compares), so the numbers and the trace panel describe the same runs.
`results_rich.json` is hand-written, has no run folders behind it, and is for page features: (two constraints, a return edge, a clustered cell, a null
extraction recall, an interval crossing 0, missing cells, markup in a rule text) and
`results_empty.json` (nothing pooled, `reproducibility` null). `golden_results_*.html` are those
two rendered by the current page; re-render them only for a page change named in the PR. The
outcome-first section is tested in `test_site_outcome_first.py`.

### Evidence panels (`panels.py`)

- **Coverage** (always): from the honesty block, the pooled constraints and, with `--ledger`, the
  full ledger. Declared, with a compiled check, exercised (at least one pooled run), exercised and
  checked, extraction recall ("not measured" when null; the `--recall` figures and the same note
  when given), and the unchecked and never-exercised
  rules by id and text. Without `--ledger` only constraints that had pooled runs can be named.
  With `--runs` as well, each never-exercised rule says whether the run folders held any of its
  runs ("No run of this rule is in the run folders this page read", or how many there are and
  that the results file counts none of them); without `--runs` no reason is given.
- **Trace at the violating action** (`--runs`, run folders or folders of them): in each
  constraint's boundary section, the first violating `mutated` run by run id beside the first
  comparison run that kept the rule (a gated run with the same operator, else a natural run;
  same task, rule and boundary, and `is_comparable`). Only runs the statistics counted are
  used: those in the rule's own `reproducibility` group (the file's top-level one for an older file
  without per-rule groups) that `replay.stats` does not set aside
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
  `.patch` beside it, one block per summary in file-name order (a folder can hold several rounds:
  the verdict, the reason a refused patch was refused, the judged numbers and the diff each
  show). Without `--repair` it says "No repair run yet". `repair` writes no
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

## Step-limit sensitivity (`step_limit.py`)

```
uv run python -m report.step_limit RUN_DIR_OR_PARENT... --ledger nemogate.yaml \
    --task ID --constraint ID --arm natural [--operator none] [--boundary ID] \
    [--repo-sha COMMIT] --out results/step-limit-sensitivity.md
```

A run that reaches the graph's step limit (`recursion_limit`) has error.json (a
`GraphRecursionError`, matched by class name) and a partial trace but no record.json, so
`replay.stats` leaves it out of the rates. This writes a two-row table for one cell: the cell as
published (loaded and pooled by `replay.stats`, so it equals the results page; `--repo-sha` pins the
task's commit exactly as `stats --repo-sha TASK=COMMIT` does) and the same cell with its step-limit
runs counted as finished. Those are counted as task failed, with the rule judged by `oracle.evaluate`
on the partial trace against the run's own `ledger.yaml` (scoped to the target rule), and the
filesystem diff taken as empty unless the folder has `fs_diff.json`; the page says which, per run.
Only step-limit runs at the published group's five reproducibility fields are counted. It is a
sensitivity check: it changes no score and the page's numbers stay those of `stats`. The partial
trace can miss a violation after the cut, so the broken count is a floor for those runs.
Reads saved folders only. Tests: `uv run pytest report/tests/test_step_limit.py`.

## Rule replay page (`replay_view.py`)

```
uv run python -m report.replay_view --out OUT.html --story TITLE \
    --rule-run RUN_DIR --gated-run RUN_DIR [--natural-run RUN_DIR] [--user-message-file FILE]
```

One rule's life as a static page of steps, from saved run folders only (no model, no network).
Stepping is radio inputs and labels, so there is no script; the first step is open, the keyboard
moves between steps with the arrow keys, and printing shows every step. The page uses the
results page's CSS plus `replay.css`, and has no font, image or link outside itself. Two builds
of the same folders are byte-identical.

- **Steps** (numbered from 1; a step the data cannot support is left out and the page says so):
  the user's rule (the user's message with the rule's text anchor marked, and the rule as the
  run's `ledger.yaml` states it); the first handoff at the run's own boundary, as sent and as
  received, with a removed sentence struck in place and tagged, a count of how often the anchor
  appears in each, and, with `--natural-run`, that run's handoff for contrast; the action that
  broke the rule (the call at the step `violations.json` names, a fork's steps counted after its
  `prefix.jsonl`, and the result that followed); and the gated run beside the sabotaged one
  (what the receiver got with the gate's re-pin shown apart from the planner's sentences, the
  calls the gate refused with its reason, the next calls, the reply, the saved verdict). Step 3
  is left out when the rule run's `violations.json` has no entry for the rule.
- **Only saved text:** every quote is read from the run folders and escaped; long text is cut
  with a visible `…` and a count of the characters left out. The verdict is `violations.json`,
  never recomputed. The text anchor comes from `replay.carriage.anchors_of` on the run's own
  check, so a rule with no text anchor marks nothing and the page says so.
- **The runs:** the footer lists them (short id, arm, operator, task, full id) and says whether
  they are forks of one source or separate runs, which are not a matched pair. The page says it
  tells one run's story, not a statistic. The runs must share task, rule and boundary.
- **The user's message** is not in a run folder. Without `--user-message-file` it is read from
  the default app's task registry (`replay.carriage.load_user_messages`, by the run's task id);
  with neither, step 1 says it is missing and shows only the ledger's rule.
- A missing or unreadable file, a trace without the handoff, or runs about different rules is
  exit code 2 with a message naming the file; nothing is written.

Tests: `uv run pytest report/tests/test_replay_view.py` (hand-built run folders in `tmp_path`).
