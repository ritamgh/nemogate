"""The results page: one self-contained static HTML file made from `results.json`.

`uv run python -m report.site --results results/results.json --out results/site/index.html`

Reads the file `replay/stats.py` writes (format_version 1, nested constraint -> boundary -> cells)
and renders a header (reproducibility group, run accounting, honesty strip), a boundary graph
(inline SVG, built only from the boundary ids in the data) and one card per constraint. Inline CSS,
no script, no font or network request. Every dynamic value goes through `html.escape`. The output
depends on the input only (no timestamp), so two runs are byte-identical. Pure local; no model call.
"""

import argparse
import html
import json
import math
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
Z = 1.96  # the stats file uses 95% Wilson intervals; the graph re-pools cells with the same z

ARM_ORDER = ("natural", "mutated", "block_only", "gated")
ARM_LABEL = {
    "natural": "Natural",
    "mutated": "Mutated",
    "block_only": "Block-only",
    "gated": "Gated (NemoGate)",
}
OPERATOR_PHRASE = {
    "drop": "lost",
    "hedge": "hedged",
    "compress_100": "compressed to 100 words",
}
REASON_LABEL = {
    "unreadable_record": "unreadable record",
    "not_comparable": "not comparable",
    "not_exercised": "rule never came up",
}
# Violation-rate bins: the upper edge of each (rate below it falls in it), then the last bin.
BIN_EDGES = (0.10, 0.30, 0.60)
BIN_LABELS = ("under 10%", "10% to under 30%", "30% to under 60%", "60% or more")
NO_DATA = "nd"

BOUNDARY_RE = re.compile(r"^(delegation|return):(.+)->(.+)$")
COMPACTION_RE = re.compile(r"^compaction:(.+)$")

NODE_W, NODE_H = 140, 60
COL_GAP, ROW_GAP = 130, 96
MARGIN_X, MARGIN_Y = 28, 24


def e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()


def pct_num(x: float) -> str:
    """Whole percent, halves rounded up (0.125 -> 13), without the sign."""
    return str(int(math.floor(x * 100 + 0.5 + 1e-9)))


def pct(x: float) -> str:
    return f"{pct_num(x)}%"


def pp(x: float) -> str:
    """Signed difference in percentage points."""
    n = int(math.floor(abs(x) * 100 + 0.5 + 1e-9))
    sign = "+" if x > 0 and n else ("-" if x < 0 and n else "")
    return f"{sign}{n} pp"


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval, the same one replay/stats.py writes per cell."""
    p = k / n
    denom = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / denom
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def rate_bin(rate: float) -> str:
    for i, edge in enumerate(BIN_EDGES):
        if rate < edge:
            return f"b{i}"
    return f"b{len(BIN_EDGES)}"


def arm_rank(arm: str) -> int:
    return ARM_ORDER.index(arm) if arm in ARM_ORDER else len(ARM_ORDER)


# --- parsing the data -------------------------------------------------------------------------


def check_format(results: dict[str, Any]) -> None:
    version = results.get("format_version")
    if version != FORMAT_VERSION:
        raise ValueError(
            f"results.json has format_version {version!r}; this page reads {FORMAT_VERSION}"
        )
    for key in ("reproducibility", "runs", "honesty", "constraints"):
        if key not in results:
            raise ValueError(f"results.json has no {key!r}")


def parse_boundary(boundary_id: str) -> tuple[str, str, str | None] | None:
    """(kind, from, to) for a handoff, ("compaction", agent, None), or None if unrecognised."""
    m = BOUNDARY_RE.match(boundary_id)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = COMPACTION_RE.match(boundary_id)
    if m:
        return "compaction", m.group(1), None
    return None


def boundary_rate(cells_lists: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Pooled violation rate at one boundary: mutated cells, else natural cells, else no data."""
    for basis in ("mutated", "natural"):
        cells = [c for cells in cells_lists for c in cells if c.get("arm") == basis]
        n = sum(c["n"] for c in cells)
        if n > 0:
            k = sum(c["violations"] for c in cells)
            lo, hi = wilson(k, n)
            return {
                "basis": basis,
                "n": n,
                "k": k,
                "rate": k / n,
                "lo": lo,
                "hi": hi,
                "clustered": any(c.get("clustered") for c in cells),
            }
    return {"basis": None, "n": 0, "k": 0, "rate": None, "lo": None, "hi": None, "clustered": False}


def collect_boundaries(constraints: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per boundary id: its pooled rate and the constraint ids (sorted) that have cells there."""
    cells_by: dict[str, list[list[dict[str, Any]]]] = {}
    owners: dict[str, list[str]] = {}
    for cid in sorted(constraints):
        for bid, body in constraints[cid].get("boundaries", {}).items():
            cells_by.setdefault(bid, []).append(body.get("cells", []))
            owners.setdefault(bid, []).append(cid)
    out = {}
    for bid in sorted(cells_by):
        info = boundary_rate(cells_by[bid])
        info["constraints"] = owners[bid]
        info["parsed"] = parse_boundary(bid)
        out[bid] = info
    return out


# --- element ids ------------------------------------------------------------------------------


class Anchors:
    """Stable, collision-free element ids: an index in sorted order plus a readable slug."""

    def __init__(self, constraints: dict[str, Any]) -> None:
        self.constraint = {cid: f"c{i}-{slug(cid)}" for i, cid in enumerate(sorted(constraints))}
        self.boundary_index: dict[str, int] = {}
        for cid in constraints:
            for bid in constraints[cid].get("boundaries", {}):
                self.boundary_index.setdefault(bid, 0)
        for i, bid in enumerate(sorted(self.boundary_index)):
            self.boundary_index[bid] = i

    def section(self, cid: str, bid: str) -> str:
        return f"{self.constraint[cid]}-b{self.boundary_index[bid]}"


# --- the boundary graph -----------------------------------------------------------------------


def layout_agents(boundaries: dict[str, dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """agent -> (layer, row). Layers follow the handoff direction; rows stack inside a layer."""
    agents: set[str] = set()
    forward: list[tuple[str, str]] = []
    for info in boundaries.values():
        p = info["parsed"]
        if not p:
            continue
        kind, a, b = p
        agents.add(a)
        if b is None:
            continue
        agents.add(b)
        if a != b:
            # a return goes back upstream: its target sits before its source
            forward.append((a, b) if kind == "delegation" else (b, a))
    layer = {a: 0 for a in agents}
    for _ in range(len(agents)):
        changed = False
        for src, dst in forward:
            if layer[dst] < layer[src] + 1 and layer[src] + 1 < len(agents):
                layer[dst] = layer[src] + 1
                changed = True
        if not changed:
            break
    rows: dict[int, int] = {}
    placed = {}
    for a in sorted(agents, key=lambda x: (layer[x], x)):
        placed[a] = (layer[a], rows.get(layer[a], 0))
        rows[layer[a]] = rows.get(layer[a], 0) + 1
    return placed


def edge_text(info: dict[str, Any]) -> str:
    if info["basis"] is None:
        return "no data"
    return pct(info["rate"]) + (" natural" if info["basis"] == "natural" else "")


def edge_title(bid: str, info: dict[str, Any]) -> str:
    owners = ", ".join(info["constraints"])
    if info["basis"] is None:
        return f"{bid}: no mutated or natural runs at this boundary. Constraints: {owners}."
    return (
        f"{bid}: {info['k']} of {info['n']} {info['basis']} runs violated the rule "
        f"({pct(info['rate'])}, 95% Wilson interval {pct_num(info['lo'])}% to "
        f"{pct_num(info['hi'])}%), pooled over all operators and constraints. "
        f"Constraints: {owners}."
    )


def render_graph(boundaries: dict[str, dict[str, Any]], anchors: Anchors) -> str:
    placed = layout_agents(boundaries)
    if not placed:
        return ""
    compaction = {
        info["parsed"][1]: (bid, info)
        for bid, info in boundaries.items()
        if info["parsed"] and info["parsed"][0] == "compaction"
    }
    max_layer = max(layer for layer, _ in placed.values())
    max_row = max(row for _, row in placed.values())

    def node_xy(agent: str) -> tuple[float, float]:
        layer, row = placed[agent]
        return MARGIN_X + layer * (NODE_W + COL_GAP), MARGIN_Y + row * (NODE_H + ROW_GAP)

    edges = []
    arc_count: dict[tuple[frozenset[str], str], int] = {}
    for bid, info in boundaries.items():
        p = info["parsed"]
        if not p or p[0] == "compaction" or p[1] == p[2]:
            continue
        kind, a, b = p
        edges.append((bid, info, kind, a, b))
    above_extent = below_extent = 0.0
    shapes = []
    for bid, info, kind, a, b in edges:
        (xa, ya), (xb, yb) = node_xy(a), node_xy(b)
        la, lb = placed[a][0], placed[b][0]
        adjacent_forward = kind == "delegation" and lb == la + 1
        if adjacent_forward:
            x1, y1, x2, y2 = xa + NODE_W, ya + NODE_H / 2, xb, yb + NODE_H / 2
            shapes.append(
                (
                    bid,
                    info,
                    kind,
                    f"M{x1:g} {y1:g}L{x2:g} {y2:g}",
                    (x1 + x2) / 2,
                    (y1 + y2) / 2,
                    (x1, y1, x2, y2),
                )
            )
            continue
        above = kind == "delegation" and lb >= la
        pair = (frozenset((a, b)), "above" if above else "below")
        stack = arc_count.get(pair, 0)
        arc_count[pair] = stack + 1
        height = 46 + 24 * abs(lb - la) + 34 * stack
        x1, x2 = xa + NODE_W / 2, xb + NODE_W / 2
        if above:
            y1, y2 = ya, yb
            cy = min(y1, y2) - 2 * height
            above_extent = max(above_extent, height)
        else:
            y1, y2 = ya + NODE_H, yb + NODE_H
            cy = max(y1, y2) + 2 * height
            below_extent = max(below_extent, height)
        cx = (x1 + x2) / 2
        mx, my = 0.25 * x1 + 0.5 * cx + 0.25 * x2, 0.25 * y1 + 0.5 * cy + 0.25 * y2
        shapes.append(
            (
                bid,
                info,
                kind,
                f"M{x1:g} {y1:g}Q{cx:g} {cy:g} {x2:g} {y2:g}",
                mx,
                my,
                (x1, y1, x2, y2),
            )
        )

    top = MARGIN_Y + (above_extent + 24 if above_extent else 0)
    shift = top - MARGIN_Y
    bottom_extra = below_extent + 24 if below_extent else 0
    width = MARGIN_X * 2 + (max_layer + 1) * NODE_W + max_layer * COL_GAP
    height = top + (max_row + 1) * NODE_H + max_row * ROW_GAP + MARGIN_Y + bottom_extra

    parts = [
        f'<svg class="graph" viewBox="0 0 {width:g} {height:g}" role="group" '
        f'aria-labelledby="graph-title" focusable="false">',
        '<title id="graph-title">Boundary graph: agents joined by the handoffs seen in the data, '
        "each handoff coloured by its pooled violation rate</title>",
        "<defs>",
    ]
    for b in [*(f"b{i}" for i in range(len(BIN_EDGES) + 1)), NO_DATA]:
        parts.append(
            f'<marker id="ah-{b}" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerUnits="userSpaceOnUse" markerWidth="13" markerHeight="13" orient="auto">'
            f'<path d="M0 0L10 5L0 10z" class="fill-{b}"/></marker>'
        )
    parts.append("</defs>")

    parts.append(f'<g transform="translate(0 {shift:g})">')
    for bid, info, kind, d, mx, my, _ in shapes:
        cls = rate_bin(info["rate"]) if info["rate"] is not None else NO_DATA
        sw = 3 if info["rate"] is None else 3 + 6 * info["rate"]
        target = anchors.section(info["constraints"][0], bid)
        text = edge_text(info)
        label = f"return {text}" if kind == "return" else text
        w = len(label) * 7.4 + 18
        parts.append(
            f'<a href="#{e(target)}" class="edge"><title>{e(edge_title(bid, info))}</title>'
            f'<path d="{d}" class="hit"/>'
            f'<path d="{d}" class="line stroke-{cls}{" dash" if cls == NO_DATA else ""}" '
            f'stroke-width="{sw:.1f}" marker-end="url(#ah-{cls})"/>'
            f'<rect x="{mx - w / 2:.1f}" y="{my - 12:.1f}" width="{w:.1f}" height="24" rx="12" '
            f'class="pill stroke-{cls}"/>'
            f'<text x="{mx:.1f}" y="{my + 4.5:.1f}" class="pilltext" text-anchor="middle">'
            f"{e(label)}</text></a>"
        )

    for agent in sorted(placed):
        x, y = node_xy(agent)
        parts.append(
            f'<g class="node"><rect x="{x:g}" y="{y:g}" width="{NODE_W}" height="{NODE_H}" rx="8"/>'
        )
        mark = compaction.get(agent)
        name_y = y + (26 if mark else 35)
        parts.append(
            f'<text x="{x + NODE_W / 2:g}" y="{name_y:g}" text-anchor="middle" class="nodename">'
            f"{e(agent)}</text>"
        )
        if mark:
            bid, info = mark
            cls = rate_bin(info["rate"]) if info["rate"] is not None else NO_DATA
            target = anchors.section(info["constraints"][0], bid)
            parts.append(
                f'<a href="#{e(target)}" class="edge"><title>{e(edge_title(bid, info))}</title>'
                f'<rect x="{x + 8:g}" y="{y + 36:g}" width="{NODE_W - 16}" height="19" rx="9.5" '
                f'class="pill stroke-{cls}"/>'
                f'<circle cx="{x + 20:g}" cy="{y + 45.5:g}" r="4.5" class="fill-{cls}"/>'
                f'<text x="{x + 30:g}" y="{y + 49.5:g}" class="marktext">'
                f"compaction {e(edge_text(info))}</text></a>"
            )
        parts.append("</g>")
    parts.append("</g></svg>")
    return "".join(parts)


def render_legend(boundaries: dict[str, dict[str, Any]]) -> str:
    items = [
        '<li><svg viewBox="0 0 34 8" aria-hidden="true"><path d="M2 4H32" '
        f'class="line stroke-b{i}" '
        f'stroke-width="{3 + 6 * (edge if i < len(BIN_EDGES) else 0.8):.1f}"/></svg>'
        f"{e(label)}</li>"
        for i, (edge, label) in enumerate(zip((*BIN_EDGES, 1), BIN_LABELS, strict=True))
    ]
    items.append(
        '<li><svg viewBox="0 0 34 8" aria-hidden="true"><path d="M2 4H32" '
        'class="line stroke-nd dash" stroke-width="3"/></svg>no data</li>'
    )
    return (
        '<ul class="legend" aria-label="Edge colour scale">' + "".join(items) + "</ul>"
        '<p class="note">Edge colour, thickness and label all give the pooled violation rate '
        "of the mutated runs at that boundary, over every constraint and operator there. "
        "Where a boundary has no mutated runs the natural runs are used and the label ends in "
        "&ldquo;natural&rdquo;. Straight edges are delegations; curved edges below the agents "
        "are returns. A boundary with neither kind of run shows &ldquo;no data&rdquo;, which "
        "is not a rate of 0.</p>"
    )


def render_boundary_table(boundaries: dict[str, dict[str, Any]], anchors: Anchors) -> str:
    rows = []
    for bid, info in boundaries.items():
        p = info["parsed"]
        kind = p[0] if p else "unrecognised"
        if info["basis"] is None:
            rate = "no data"
        else:
            rate = (
                f"{pct(info['rate'])} ({pct_num(info['lo'])}&ndash;{pct_num(info['hi'])}%), "
                f"{e(info['basis'])} runs"
            )
        links = ", ".join(
            f'<a href="#{e(anchors.section(cid, bid))}">{e(cid)}</a>' for cid in info["constraints"]
        )
        drawn = "" if p and (p[0] == "compaction" or p[1] != p[2]) else " (not drawn)"
        rows.append(
            f'<tr><th scope="row"><code>{e(bid)}</code>{drawn}</th><td>{e(kind)}</td>'
            f'<td class="num">{rate}</td><td class="num">{info["n"] or "&ndash;"}</td>'
            f"<td>{links}</td></tr>"
        )
    return (
        '<div class="tablewrap"><table class="plain"><caption>Every boundary in the data, as text'
        '</caption><thead><tr><th scope="col">Boundary</th><th scope="col">Kind</th>'
        '<th scope="col">Pooled violation rate</th><th scope="col">n</th>'
        '<th scope="col">Constraint cards</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


# --- header -----------------------------------------------------------------------------------


def short(value: Any) -> str:
    text = str(value)
    return f'<code title="{e(text)}">{e(text[:12])}</code>'


def render_header(results: dict[str, Any]) -> str:
    repro = results.get("reproducibility") or {}
    models = repro.get("model_ids") or {}
    model_items = (
        "".join(
            f"<div><dt>{e(role)}</dt><dd><code>{e(mid)}</code></dd></div>"
            for role, mid in sorted(models.items())
        )
        or "<div><dt>models</dt><dd>none recorded</dd></div>"
    )
    hashes = "".join(
        f"<div><dt>{e(label)}</dt>"
        f"<dd>{short(repro[key]) if repro.get(key) else 'none recorded'}</dd></div>"
        for key, label in (
            ("safe_config_hash", "safe config"),
            ("ledger_hash", "ledger"),
            ("repo_sha", "repo commit"),
            ("lockfile_hash", "lockfile"),
        )
    )
    runs = results["runs"]
    reasons = runs.get("inconclusive", {})
    reason_items = "".join(
        f"<div><dt>{e(REASON_LABEL.get(r, r.replace('_', ' ')))}</dt><dd>{e(reasons[r])}</dd></div>"
        for r in sorted(reasons)
    )
    honesty = results["honesty"]
    recall = honesty.get("extraction_recall")
    recall_text = "not measured" if recall is None else pct(recall)
    total, with_check = (
        honesty.get("constraints_total", 0),
        honesty.get("constraints_with_check", 0),
    )
    inconclusive_total = honesty.get("inconclusive_total", 0)
    if results["reproducibility"]:
        group_note = "Only runs sharing all of these, and the hashes beside them, were pooled."
    else:
        group_note = "No runs were pooled, so there is no reproducibility group."
    return f"""<header class="top">
<h1>NemoGate results</h1>
<p class="lead">Do the rules survive the handoffs between agents? Each boundary below was tested by
sabotaging the handoff and counting how often the rule was then broken.</p>
</header>
<section aria-labelledby="honesty-h" class="strip">
<h2 id="honesty-h">What these numbers can and cannot tell you</h2>
<ul class="honesty">
<li><span class="v">{e(recall_text)}</span>
<span class="k">extraction recall: how many of the seeded rules the extractor found</span></li>
<li><span class="v">{e(with_check)} of {e(total)}</span>
<span class="k">constraints have a compiled check; only those can be graded</span></li>
<li><span class="v">{e(inconclusive_total)}</span>
<span class="k">inconclusive runs, left out of every rate below</span></li>
</ul>
</section>
<section aria-labelledby="runs-h" class="meta">
<div>
<h2 id="runs-h">Runs</h2>
<dl class="kv"><div><dt>found</dt><dd>{e(runs.get("found", 0))}</dd></div>
<div><dt>pooled into a cell</dt><dd>{e(runs.get("pooled", 0))}</dd></div></dl>
<h3>Left out, by reason</h3>
<dl class="kv">{reason_items}</dl>
</div>
<div>
<h2>Models</h2>
<p class="note">{group_note}</p>
<dl class="kv">{model_items}</dl>
</div>
<div>
<h2>Reproducibility group</h2>
<dl class="kv">{hashes}</dl>
</div>
</section>"""


# --- constraint cards -------------------------------------------------------------------------


def interval_bar(rate: float, lo: float, hi: float, signed: bool = False) -> str:
    """A small bar: the shaded part is the interval, the tick is the estimate. Decorative."""
    if signed:
        lo_s, hi_s, r_s = (v / 2 + 0.5 for v in (lo, hi, rate))
        zero = '<line x1="50" x2="50" y1="0" y2="10" class="zero"/>'
    else:
        lo_s, hi_s, r_s = lo, hi, rate
        zero = ""
    return (
        '<svg class="ibar" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">'
        '<rect x="0" y="3" width="100" height="4" class="track"/>'
        f'<rect x="{lo_s * 100:.2f}" y="1" width="{max((hi_s - lo_s) * 100, 0.8):.2f}" height="8" '
        'class="span"/>'
        f"{zero}"
        f'<rect x="{min(max(r_s * 100 - 0.7, 0), 98.6):.2f}" y="0" width="1.4" height="10" '
        'class="tick"/>'
        "</svg>"
    )


def fraction_cell(count: int, n: int, stat: dict[str, Any]) -> str:
    return (
        f'<span class="rate">{pct(stat["rate"])}</span> '
        '<span class="ci" title="95% Wilson interval">'
        f"({pct_num(stat['lo'])}&ndash;{pct_num(stat['hi'])}%)</span>"
        f'<span class="count">{e(count)} of {e(n)} runs</span>'
        + interval_bar(stat["rate"], stat["lo"], stat["hi"])
    )


def cell_row(arm: str, op: str, cell: dict[str, Any] | None) -> str:
    label = f'{e(ARM_LABEL.get(arm, arm))}</th><td data-label="Operator">{e(op)}</td>'
    if cell is None:
        return (
            f'<tr class="missing"><th scope="row">{label}'
            '<td colspan="3" class="notrun">not run</td></tr>'
        )
    n = cell["n"]
    n_text = f"{e(n)}"
    warn = ""
    if cell.get("clustered"):
        n_text += f'<span class="count">from {e(cell.get("n_sources"))} sources</span>'
        warn = (
            '<span class="warn"><strong>Clustered.</strong> Forked runs share a source run, so '
            "they are not independent and these intervals are too narrow.</span>"
        )
    cvr = fraction_cell(cell["violations"], n, cell["cvr"])
    success = fraction_cell(cell["successes"], n, cell["success"])
    return (
        f'<tr><th scope="row">{label}<td data-label="Runs">{n_text}{warn}</td>'
        f'<td data-label="Violation rate">{cvr}</td>'
        f'<td data-label="Task success">{success}</td></tr>'
    )


def find_cell(cells: list[dict[str, Any]], arm: str, op: str) -> dict[str, Any] | None:
    for c in cells:
        if c.get("arm") == arm and c.get("operator") == op:
            return c
    return None


def expected_rows(cells: list[dict[str, Any]]) -> list[tuple[str, str]]:
    ops = sorted(
        {c["operator"] for c in cells if c["arm"] != "natural" and c["operator"] != "none"}
    )
    rows = {("natural", "none")}
    for arm in ("mutated", "block_only", "gated"):
        rows.update((arm, op) for op in ops)
    rows.update((c["arm"], c["operator"]) for c in cells)
    return sorted(rows, key=lambda r: (arm_rank(r[0]), r[1]))


COMPARISONS = (
    (
        "load_bearing",
        "Load-bearing score",
        "violation rate with the rule sabotaged (mutated) minus the natural violation rate",
        "mutated",
    ),
    (
        "ledger_value",
        "Ledger value",
        "task success with the rule carried (gated) minus task success with blocking alone",
        "gated",
    ),
)


def render_comparisons(cells: list[dict[str, Any]], comparisons: list[dict[str, Any]]) -> str:
    if not any(c["arm"] in ("mutated", "gated") for c in cells):
        return (
            '<p class="note"><span class="notrun">Load-bearing score and ledger value: not run'
            "</span> (no mutated or gated runs at this boundary).</p>"
        )
    items = []
    for name, title, meaning, arm in COMPARISONS:
        ops = sorted({c["operator"] for c in cells if c["arm"] == arm}) or [None]
        for op in ops:
            comp = next(
                (c for c in comparisons if c.get("name") == name and c.get("operator") == op), None
            )
            head = f"{e(title)}" + (f" ({e(op)})" if op else "")
            if comp is None:
                body = '<span class="notrun">not run</span>'
            else:
                crosses = comp["lo"] <= 0 <= comp["hi"]
                verdict = (
                    "Not distinguishable from 0 at these sample sizes."
                    if crosses
                    else ("Higher" if comp["diff"] > 0 else "Lower")
                    + " than 0: the interval excludes 0."
                )
                body = (
                    f'<span class="rate">{e(pp(comp["diff"]))}</span> '
                    f'<span class="ci">(95% interval {e(pp(comp["lo"]))} to '
                    f"{e(pp(comp['hi']))})</span> "
                    f'<span class="verdict{" zero" if crosses else ""}">{e(verdict)}</span>'
                    + interval_bar(comp["diff"], comp["lo"], comp["hi"], signed=True)
                )
            items.append(f"<div><dt>{head}<small>{e(meaning)}</small></dt><dd>{body}</dd></div>")
    return '<dl class="compare">' + "".join(items) + "</dl>"


def summaries(boundaries: dict[str, Any]) -> list[str]:
    """One sentence per (boundary, operator) where mutated CVR, block-only success and gated
    success all exist. Nothing is estimated: a missing number means no sentence."""
    out = []
    for bid in sorted(boundaries):
        cells = boundaries[bid].get("cells", [])
        parsed = parse_boundary(bid)
        who = f"the {e(parsed[2])}" if parsed and parsed[2] else "the system"
        for op in sorted({c["operator"] for c in cells if c["arm"] == "mutated"}):
            mut = find_cell(cells, "mutated", op)
            blk = find_cell(cells, "block_only", op)
            gat = find_cell(cells, "gated", op)
            if not (mut and blk and gat):
                continue
            phrase = OPERATOR_PHRASE.get(op, f"altered by {op}")
            out.append(
                f"With the rule {e(phrase)} at the handoff <code>{e(bid)}</code>, {who} broke it "
                f"{pct(mut['cvr']['rate'])} of the time "
                f"({e(mut['violations'])} of {e(mut['n'])} runs). "
                f"Blocking alone left task success at {pct(blk['success']['rate'])}. "
                f"NemoGate carried the rule across: task success {pct(gat['success']['rate'])}."
            )
    return out


def render_constraint(cid: str, body: dict[str, Any], anchors: Anchors) -> str:
    check = "compiled check" if body.get("has_check") else "no compiled check"
    check_cls = "yes" if body.get("has_check") else "no"
    sentences = summaries(body.get("boundaries", {}))
    summary = "".join(f'<p class="summary">{s}</p>' for s in sentences)
    tasks = body.get("task_ids") or []
    task_line = (
        f'<p class="note">Tasks that contributed runs: {", ".join(e(t) for t in tasks)}.</p>'
        if tasks
        else ""
    )
    sections = []
    for bid in sorted(body.get("boundaries", {})):
        b = body["boundaries"][bid]
        cells = b.get("cells", [])
        rows = "".join(
            cell_row(arm, op, find_cell(cells, arm, op)) for arm, op in expected_rows(cells)
        )
        sid = e(anchors.section(cid, bid))
        sections.append(
            f'<section class="bnd" id="{sid}" aria-labelledby="{sid}-h">'
            f'<h4 id="{sid}-h"><code>{e(bid)}</code></h4>'
            '<div class="tablewrap"><table class="cells"><colgroup><col style="width:21%">'
            '<col style="width:13%"><col style="width:18%"><col style="width:24%">'
            '<col style="width:24%">'
            "</colgroup><thead><tr>"
            '<th scope="col">Arm</th><th scope="col">Operator</th><th scope="col">Runs</th>'
            '<th scope="col">Violation rate</th><th scope="col">Task success</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
            + render_comparisons(cells, b.get("comparisons", []))
            + "</section>"
        )
    return (
        f'<article class="constraint" id="{e(anchors.constraint[cid])}" '
        f'aria-labelledby="{e(anchors.constraint[cid])}-h">'
        f'<header><h3 id="{e(anchors.constraint[cid])}-h"><code>{e(cid)}</code></h3>'
        f'<span class="badge">{e(body.get("kind", "unknown kind"))}</span>'
        f'<span class="badge {check_cls}">{check}</span></header>'
        f'<p class="rule">{e(body.get("text", ""))}</p>{summary}{task_line}{"".join(sections)}'
        "</article>"
    )


# --- page -------------------------------------------------------------------------------------

CSS = (Path(__file__).parent / "site.css").read_text(encoding="utf-8")


def render(results: dict[str, Any]) -> str:
    check_format(results)
    constraints = results["constraints"]
    anchors = Anchors(constraints)
    boundaries = collect_boundaries(constraints)
    graph = render_graph(boundaries, anchors)
    if constraints:
        graph_section = (
            '<section aria-labelledby="graph-h"><h2 id="graph-h">Boundary graph</h2>'
            '<div class="graphwrap" tabindex="0" role="region" '
            'aria-label="Boundary graph, scrolls sideways on narrow screens">'
            f"{graph}</div>{render_legend(boundaries)}"
            f"{render_boundary_table(boundaries, anchors)}</section>"
            '<section aria-labelledby="cards-h"><h2 id="cards-h">Constraints</h2>'
            + "".join(
                render_constraint(cid, constraints[cid], anchors) for cid in sorted(constraints)
            )
            + "</section>"
        )
    else:
        graph_section = (
            '<div class="empty"><strong>No runs were pooled.</strong> There is nothing to chart '
            "yet: no boundary, no constraint and no rate. Nothing here is a zero.</div>"
        )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        "<title>NemoGate results</title>"
        f"<style>{CSS}</style></head><body><main>"
        f"{render_header(results)}{graph_section}"
        '<footer><p class="note">Intervals are 95% Wilson score intervals. A difference uses the '
        "Newcombe hybrid-score interval. Cells are pooled only within one reproducibility group. "
        "Whether a rule was obeyed comes from deterministic checks, never from a model. Format "
        f"version {e(results['format_version'])}.</p></footer>"
        "</main></body></html>\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render results.json as one static HTML page.")
    parser.add_argument(
        "--results", type=Path, required=True, help="results.json from replay.stats"
    )
    parser.add_argument("--out", type=Path, required=True, help="where to write index.html")
    args = parser.parse_args(argv)
    try:
        results = json.loads(args.results.read_text(encoding="utf-8"))
        page = render(results)
    except (OSError, ValueError) as exc:
        print(f"report.site: {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
