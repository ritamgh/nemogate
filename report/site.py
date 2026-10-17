"""The results page: one self-contained static HTML file made from `results.json`.

`uv run python -m report.site --results results/results.json --out results/site/index.html
[--runs DIR...] [--repair DIR] [--ledger nemogate.yaml]`

Reads the file `replay/stats.py` writes (format_version 2, nested constraint -> boundary -> cells)
and renders a header (reproducibility group, run accounting, honesty strip), a boundary graph
(inline SVG, built only from the boundary ids in the data) and one card per constraint. The
evidence panels (coverage, the trace at the violating action, the repair patch) live in
`report/panels.py`. Inline CSS,
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

from report import panels
from schemas.ledger import Ledger, load_ledger_yaml

FORMAT_VERSION = 2
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
COL_GAP, ROW_GAP = 150, 96
MARGIN_X, MARGIN_Y = 28, 24
CLUSTER_WARNING = (
    "Clustered: includes forked runs that share a source run, so this interval is too narrow."
)
CLUSTER_NOTE = (  # the same warning where it is visible text rather than a tooltip
    "Not independent: includes forked runs that share a source run, so this range is too narrow."
)
KEY_ARMS = (
    ("Natural", "no sabotage: the agents hand the rule on, or lose it, by themselves."),
    ("Mutated", "the handoff is sabotaged by an operator and nothing guards the rule."),
    (
        "Block-only",
        "sabotaged handoff, and the gate refuses an action that breaks the rule, without "
        "sending the rule again.",
    ),
    (
        "Gated",
        "sabotaged handoff, and the gate both refuses a rule-breaking action and puts the "
        "rule back into the handoff.",
    ),
)
KEY_OPERATORS = (
    ("drop", "the sentence that states the rule is removed from the handoff."),
    ("hedge", "the rule is reworded as vague advice."),
    ("compress_100", "the handoff is summarised to 100 words."),
)
FIXES = (
    (
        "The gate (block-only and gated arms)",
        "blocks a tool call that breaks a rule it can check before the call (path, tool and "
        "approval rules), whatever the cause, so it also stops a rule that arrived but was "
        "ignored. Gated also holds back a reply that misses an output rule.",
    ),
    (
        "Reading a 0 in the blocking arms",
        "For a path, tool or approval rule, a 0 broken in the block-only and gated arms comes "
        "from blocking: the gate refuses the action with the same check that grades the run, so "
        "both arms get it by construction. Other rules are not refused before the call (frozen "
        "files and output rules are graded after the run), so a 0 there is a measured result, "
        "not a construction. Whether carrying the rule adds anything over blocking, such as "
        "finishing the task where blocking leaves the agent stuck, is the hypothesis being "
        "measured, not a result.",
    ),
    (
        "Repair",
        "Repair targets rules lost in the handoff. Its fixes are instructions that make sure the "
        "rule reaches the agent.",
    ),
    (
        "Checked after the run",
        "Rules checked on the files an agent changed, such as frozen files, are graded after the "
        "run. The gate does not block them.",
    ),
)
SIDE_ARC = 52  # how far a same-column edge bows out to the right


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


def is_graded(body: dict[str, Any]) -> bool:
    """A rule can be graded only if it has a compiled check; without one no run can be flagged,
    so its violation counts would be zeros that mean nothing."""
    return bool(body.get("has_check"))


def collect_boundaries(constraints: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per boundary id: its pooled rate (graded constraints only) and the constraint ids (sorted)
    that have cells there, graded or not."""
    cells_by: dict[str, list[list[dict[str, Any]]]] = {}
    owners: dict[str, list[str]] = {}
    for cid in sorted(constraints):
        graded = is_graded(constraints[cid])
        for bid, body in constraints[cid].get("boundaries", {}).items():
            cells_by.setdefault(bid, []).extend([body.get("cells", [])] if graded else [])
            owners.setdefault(bid, []).append(cid)
    out = {}
    for bid in sorted(owners):
        info = boundary_rate(cells_by[bid])
        info["constraints"] = owners[bid]
        info["graded"] = [c for c in owners[bid] if is_graded(constraints[c])]
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


def rate_text(info: dict[str, Any]) -> str:
    if info["basis"] is None:
        return "no data"
    return pct(info["rate"]) + (" natural" if info["basis"] == "natural" else "")


def edge_text(info: dict[str, Any]) -> str:
    """The pill on an edge: the rate and the runs behind it, so a pooled 50% is not read as the
    5 of 10 on a card."""
    if info["basis"] is None:
        return "no data"
    counts = f" {info['k']}/{info['n']}"
    return pct(info["rate"]) + counts + (" natural" if info["basis"] == "natural" else "")


def link_target(info: dict[str, Any], anchors: Anchors, bid: str) -> str:
    """The card an edge links to: the first constraint that was graded here, else the first."""
    return anchors.section((info["graded"] or info["constraints"])[0], bid)


def edge_title(bid: str, info: dict[str, Any]) -> str:
    owners = ", ".join(info["constraints"])
    if info["basis"] is None:
        return f"{bid}: no graded mutated or natural runs at this boundary. Constraints: {owners}."
    return (
        f"{bid}: {info['k']} of {info['n']} {info['basis']} runs violated the rule "
        f"({pct(info['rate'])}, 95% Wilson interval {pct_num(info['lo'])}% to "
        f"{pct_num(info['hi'])}%), pooled over all operators and constraints. "
        f"{CLUSTER_WARNING + ' ' if info['clustered'] else ''}"
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
    above_extent = below_extent = side_extent = 0.0
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
        above = kind == "delegation" and lb > la
        side = la == lb
        pair = (frozenset((a, b)), "side" if side else ("above" if above else "below"))
        stack = arc_count.get(pair, 0)
        arc_count[pair] = stack + 1
        if side:
            # two agents in one column: bow out to the right, into the gap, clear of both nodes
            offset = SIDE_ARC + 30 * stack
            x1 = x2 = xa + NODE_W
            y1, y2 = ya + NODE_H / 2, yb + NODE_H / 2
            cx, cy = x1 + 2 * offset, (y1 + y2) / 2
            side_extent = max(side_extent, x1 + offset + 60)
            shapes.append(
                (
                    bid,
                    info,
                    kind,
                    f"M{x1:g} {y1:g}Q{cx:g} {cy:g} {x2:g} {y2:g}",
                    x1 + offset,
                    cy,
                    (x1, y1, x2, y2),
                )
            )
            continue
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
    width = max(
        MARGIN_X * 2 + (max_layer + 1) * NODE_W + max_layer * COL_GAP, side_extent + MARGIN_X
    )
    height = top + (max_row + 1) * NODE_H + max_row * ROW_GAP + MARGIN_Y + bottom_extra

    parts = [
        f'<svg class="graph" viewBox="0 0 {width:g} {height:g}" role="group" '
        f'aria-labelledby="graph-title" focusable="false">',
        '<title id="graph-title">Boundary graph: agents joined by the handoffs seen in the data, '
        "each handoff coloured by its violation rate</title>",
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
        target = link_target(info, anchors, bid)
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
            target = link_target(info, anchors, bid)
            parts.append(
                f'<a href="#{e(target)}" class="edge"><title>{e(edge_title(bid, info))}</title>'
                f'<rect x="{x + 8:g}" y="{y + 36:g}" width="{NODE_W - 16}" height="19" rx="9.5" '
                f'class="pill stroke-{cls}"/>'
                f'<circle cx="{x + 20:g}" cy="{y + 45.5:g}" r="4.5" class="fill-{cls}"/>'
                f'<text x="{x + 30:g}" y="{y + 49.5:g}" class="marktext">'
                f"compaction {e(rate_text(info))}</text></a>"
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
        '<p class="note">Edge colour, thickness and label all give how often the rule was broken '
        "in the mutated runs at that boundary, over every graded constraint and operator there; "
        "the label also gives the runs behind it (broken/total). "
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
            rate = "no data" if info["graded"] else "no data (no graded constraint here)"
        else:
            rate = (
                f"{pct(info['rate'])} ({pct_num(info['lo'])}&ndash;{pct_num(info['hi'])}%), "
                f"{e(info['basis'])} runs"
            )
            if info["clustered"]:
                rate += f' <span class="warn">{e(CLUSTER_NOTE)}</span>'
        links = ", ".join(
            f'<a href="#{e(anchors.section(cid, bid))}">{e(cid)}</a>' for cid in info["constraints"]
        )
        drawn = "" if p and (p[0] == "compaction" or p[1] != p[2]) else " (not drawn)"
        rows.append(
            f'<tr><th scope="row"><code>{e(bid)}</code>{drawn}</th><td>{e(kind)}</td>'
            f'<td class="num">{rate}</td><td class="num">{info["n"] or "&ndash;"}</td>'
            f"<td>{links}</td></tr>"
        )
    return panels.tablewrap(
        "Every boundary in the data, as a table",
        '<table class="plain"><caption>Every boundary in the data, as text'
        '</caption><thead><tr><th scope="col">Boundary</th><th scope="col">Kind</th>'
        '<th scope="col">Violation rate, all graded constraints</th><th scope="col">n</th>'
        '<th scope="col">Constraint cards</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>",
    )


# --- header -----------------------------------------------------------------------------------


def short(value: Any) -> str:
    text = str(value)
    return f'<code title="{e(text)}">{e(text[:12])}</code>'


def errored_detail(row: dict[str, Any]) -> str:
    """ "2 runs, 1 with no recorded cost" for an `errored_cost` row (the total or one arm)."""
    unknown = row.get("cost_unknown", 0)
    plural = "run" if row["count"] == 1 else "runs"
    return f"{row['count']} {plural}" + (f", {unknown} with no recorded cost" if unknown else "")


def render_header(results: dict[str, Any], verdict: str) -> str:
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
    errored = runs.get("errored_cost") or {}
    errored_items = ""
    if errored.get("count"):
        detail = errored_detail(errored)
        per_arm = "".join(
            f"<div><dt>errored, {e(ARM_LABEL.get(arm, arm))}</dt>"
            f"<dd>{e(money(row['cost_usd']))} ({e(errored_detail(row))})</dd></div>"
            for arm, row in sorted((errored.get("by_arm") or {}).items())
        )
        errored_items = (
            "<h3>Money in no rate</h3>"
            '<dl class="kv"><div><dt>spent on errored runs</dt>'
            f"<dd>{e(money(errored['cost_usd']))} ({e(detail)})</dd></div>{per_arm}</dl>"
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
        group_note = "Only runs made under exactly these conditions were counted."
    else:
        group_note = "No runs were counted, so there is no shared set of conditions."
    return f"""<header class="top">
<h1>NemoGate results</h1>
<p class="lead">Do the rules survive the handoffs between agents? Each boundary below was tested by
sabotaging the handoff and counting how often the rule was then broken.</p>
{verdict}
</header>
{render_key()}{render_fixes()}
<section aria-labelledby="honesty-h" class="strip">
<h2 id="honesty-h">What these numbers can and cannot tell you</h2>
<ul class="honesty">
<li><span class="v">{e(recall_text)}</span>
<span class="k">extraction recall: how many of the seeded rules the extractor found</span></li>
<li><span class="v">{e(with_check)} of {e(total)}</span>
<span class="k">rules can be graded: each has a compiled check, a program that decides
pass or fail</span></li>
<li><span class="v">{e(inconclusive_total)}</span>
<span class="k">inconclusive runs, left out of every rate below. Natural runs where the
rule never came up are not among them: they stay in the rates, and each natural row counts
them</span></li>
</ul>
</section>
<section aria-labelledby="runs-h" class="meta">
<div>
<h2 id="runs-h">Runs</h2>
<dl class="kv"><div><dt>found</dt><dd>{e(runs.get("found", 0))}</dd></div>
<div><dt>counted in the rates</dt><dd>{e(runs.get("pooled", 0))}</dd></div></dl>
<h3>Left out, by reason</h3>
<dl class="kv">{reason_items}</dl>{errored_items}
</div>
<div>
<h2>Models</h2>
<p class="note">{group_note}</p>
<dl class="kv">{model_items}</dl>
</div>
<div>
<h2>Same conditions</h2>
<dl class="kv">{hashes}</dl>
</div>
</section>"""


# --- verdict and key --------------------------------------------------------------------------


BLOCKING_ZERO = (
    " That 0 comes from blocking: the block-only arm refuses the same action with the same "
    "check, so it gets 0 by construction and the figure does not show that carrying the rule "
    "helps."
)


def blocking_made_the_zero(body: dict[str, Any], gated: dict[str, Any]) -> bool:
    """Is a gated cell's 0 broken something blocking can explain? Only for a rule the gate refuses
    before the call (`blocked_before_call`, from the gate's own policy), and not when the cell's
    traces show no gated run was blocked at all."""
    if gated["violations"] != 0 or not body.get("blocked_before_call"):
        return False
    blocks = (gated.get("arm_metrics") or {}).get("blocks")
    return blocks is None or blocks["runs_with_block"] > 0


def render_verdict(results: dict[str, Any], anchors: Anchors) -> str:
    """One plain sentence from the data: the largest rule effect (sabotaged versus natural
    violation rate) and what the gated runs did in the same case. Only graded constraints count.
    Where an interval includes 0 the sentence says it cannot be told from 0."""
    found = []
    for cid, body in sorted(results["constraints"].items()):
        if not is_graded(body):
            continue
        for bid, b in sorted(body.get("boundaries", {}).items()):
            cells = b.get("cells", [])
            for comp in b.get("comparisons", []):
                if comp.get("name") != "load_bearing":
                    continue
                knobs = comp.get("compaction", ANY)
                mut = find_cell(cells, comp["a"]["arm"], comp["a"]["operator"], knobs)
                nat = find_cell(cells, comp["b"]["arm"], comp["b"]["operator"], knobs)
                if mut and nat:
                    found.append((cid, bid, comp, mut, nat, cells))
    if not found:
        return (
            '<p class="verdict-line"><strong>No comparison has enough runs yet.</strong> A rule '
            "effect needs a rule with a compiled check and runs both with and without the "
            "sabotage at the same handoff.</p>"
        )
    clear = [f for f in found if not f[2]["lo"] <= 0 <= f[2]["hi"]]
    cid, bid, comp, mut, nat, cells = min(
        clear or found, key=lambda f: (-f[2]["diff"], f[0], f[1], f[2]["operator"])
    )
    op = comp["operator"]
    tail = (
        f", {indistinct(comp, cells)}" if comp["lo"] <= 0 <= comp["hi"] else ""
    )  # a win claim whose interval includes 0 says so
    gat = find_cell(cells, "gated", op, comp.get("compaction", ANY))
    gated = (
        f"With the gate on, it was broken in {e(gat['violations'])} of {e(gat['n'])} gated runs "
        f"({pct(gat['cvr']['rate'])}, 95% interval {pct_num(gat['cvr']['lo'])}% to "
        f"{pct_num(gat['cvr']['hi'])}%)."
        + (BLOCKING_ZERO if blocking_made_the_zero(results["constraints"][cid], gat) else "")
        if gat
        else "There are no gated runs of this case yet."
    )
    phrase = OPERATOR_PHRASE.get(op, f"altered by {op}")
    return (
        '<p class="verdict-line"><strong>Largest effect found.</strong> '
        f'At <a href="#{e(anchors.section(cid, bid))}"><code>{e(cid)}</code> on '
        f"<code>{e(bid)}</code></a>, with the rule {e(phrase)}, it was broken in "
        f"{e(mut['violations'])} of {e(mut['n'])} runs ({pct(mut['cvr']['rate'])}) against "
        f"{e(nat['violations'])} of {e(nat['n'])} with no sabotage ({pct(nat['cvr']['rate'])}): "
        f"{e(pp(comp['diff']))} (95% interval {e(pp(comp['lo']))} to {e(pp(comp['hi']))}"
        f"{e(tail)}). {gated}</p>"
    )


def render_key() -> str:
    arms = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in KEY_ARMS)
    ops = "".join(
        f"<div><dt><code>{e(k)}</code></dt><dd>{e(v)}</dd></div>" for k, v in KEY_OPERATORS
    )
    return (
        '<section aria-labelledby="key-h" class="key"><h2 id="key-h">Key</h2><div class="keycols">'
        f'<div><h3>Arms (how a run was set up)</h3><dl class="kv stack">{arms}</dl></div>'
        f'<div><h3>Operators (how the handoff is sabotaged)</h3><dl class="kv stack">{ops}</dl>'
        "</div></div></section>"
    )


def render_fixes() -> str:
    items = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in FIXES)
    return (
        '<section aria-labelledby="fixes-h" class="key"><h2 id="fixes-h">What each part fixes</h2>'
        f'<dl class="kv stack">{items}</dl></section>'
    )


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


def split_note(cell: dict[str, Any]) -> str:
    """For a natural cell with `violations_by_label`: why the broken runs broke, in words.

    A rule dropped in the handoff is what the mutated arm models; one carried and broken anyway
    is plain disobedience. Empty when the field is absent or empty (older results files).
    """
    split = cell.get("violations_by_label") or {}
    dropped = split.get("DROPPED", 0)
    disobeyed = split.get("DISOBEYED", 0)
    other = sum(split.values()) - dropped - disobeyed
    parts = []
    if dropped:
        parts.append(f"{dropped} lost in the handoff")
    if disobeyed:
        parts.append(f"{disobeyed} carried but disobeyed")
    if other:
        parts.append(f"{other} not classified")
    if not parts:
        return ""
    return (
        f'<span class="count split">{e(cell["violations"])} of {e(cell["n"])} broken: '
        f"{e(', '.join(parts))}</span>"
    )


def unexercised_note(cell: dict[str, Any]) -> str:
    """For a natural cell: how many of its runs never asked for the guarded action. They are in
    `n` (and so in the rate), so a reader needs the count to know how often the rule came up."""
    count = cell.get("not_exercised", 0)
    if not count:
        return ""
    return f'<span class="count">the rule never came up in {e(count)} of {e(cell["n"])} runs</span>'


def cell_row(arm: str, op: str, cell: dict[str, Any] | None, note: str = "") -> str:
    label = (
        f'{e(ARM_LABEL.get(arm, arm))}</th><td data-label="Operator">{e(op)}'
        + (f'<span class="count">{e(note)}</span>' if note else "")
        + "</td>"
    )
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
            '<span class="warn"><strong>Not independent.</strong> Forked runs share a source run, '
            "so these ranges are too narrow.</span>"
        )
    cvr = fraction_cell(cell["violations"], n, cell["cvr"])
    if cell.get("arm") == "natural":
        cvr += split_note(cell) + unexercised_note(cell)
    success = fraction_cell(cell["successes"], n, cell["success"])
    return (
        f'<tr><th scope="row">{label}<td data-label="Runs">{n_text}{warn}</td>'
        f'<td data-label="Violation rate">{cvr}</td>'
        f'<td data-label="Task success">{success}</td></tr>'
    )


ANY = object()  # find_cell: match whatever compaction setting a cell has


def find_cell(
    cells: list[dict[str, Any]], arm: str, op: str, compaction: Any = ANY
) -> dict[str, Any] | None:
    """The first cell for (arm, operator) under `compaction` (the cell's own `compaction` value:
    None = not recorded). `replay.stats` lists the compaction-off cell first."""
    for c in cells:
        if c.get("arm") == arm and c.get("operator") == op:
            if compaction is ANY or c.get("compaction") == compaction:
                return c
    return None


def compaction_settings(cells: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """The distinct compaction settings of `cells`: off first, then not recorded, then on."""
    settings: list[dict[str, Any] | None] = []
    for c in cells:
        if c.get("compaction") not in settings:
            settings.append(c.get("compaction"))

    def rank(setting: dict[str, Any] | None) -> tuple[int, int, int]:
        if setting is None:
            return (1, 0, 0)
        tokens = setting.get("tokens")
        return (0 if tokens is None else 2, tokens or 0, setting.get("keep") or 0)

    return sorted(settings, key=rank)


def compaction_note(compaction: dict[str, Any] | None) -> str:
    """How the planner was compacted in a cell's runs, for the row label."""
    if compaction is None:
        return "compaction not recorded"
    if compaction.get("tokens") is None:
        return "compaction off"
    return (
        f"compaction at {compaction['tokens']} tokens, keep {compaction.get('keep') or 'default'}"
    )


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
        "Rule effect (violations added by the sabotage)",
        "violation rate with the rule sabotaged (mutated) minus the natural violation rate",
        "mutated",
    ),
    (
        "ledger_value",
        "Gate effect on task success",
        "task success with the rule carried (gated) minus task success with blocking alone",
        "gated",
    ),
)


def n_text(*counts: int) -> str:
    """ "n = 10" when the groups compared are the same size, else "n = 10 and 8"."""
    return "n = " + " and ".join(dict.fromkeys(str(c) for c in counts))


def compared_n(cells: list[dict[str, Any]], comp: dict[str, Any]) -> list[int]:
    """The run counts of the two cells a comparison is made from (empty if a cell is missing)."""
    knobs = comp.get("compaction", ANY)
    pair = [find_cell(cells, comp[side]["arm"], comp[side]["operator"], knobs) for side in "ab"]
    return [c["n"] for c in pair if c]


def indistinct(comp: dict[str, Any], cells: list[dict[str, Any]]) -> str:
    """Plain wording for an interval that includes 0, with the run counts it rests on."""
    counts = compared_n(cells, comp)
    return "not distinguishable from 0 at " + (n_text(*counts) if counts else "these sample sizes")


def render_comparisons(cells: list[dict[str, Any]], comparisons: list[dict[str, Any]]) -> str:
    if not any(c["arm"] in ("mutated", "gated") for c in cells):
        return (
            '<p class="note"><span class="notrun">Rule effect and gate effect: not run'
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
                    indistinct(comp, cells).capitalize() + "."
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


def render_comparisons_by_setting(
    cells: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
    settings: list[dict[str, Any] | None],
) -> str:
    """The effect comparisons of each compaction setting that has mutated or gated runs, each
    under its own heading (a setting with only natural runs has nothing to compare)."""
    blocks = []
    for setting in settings:
        here = [c for c in cells if c.get("compaction") == setting]
        if any(c["arm"] in ("mutated", "gated") for c in here):
            mine = [c for c in comparisons if c.get("compaction") == setting]
            blocks.append(
                f'<p class="note">Under <strong>{e(compaction_note(setting))}</strong>:</p>'
                + render_comparisons(here, mine)
            )
    return "".join(blocks) or render_comparisons(cells, comparisons)


def gate_effect(boundaries: dict[str, Any], bid: str, op: str, blk: dict, gat: dict) -> str:
    """The gated-versus-block-only task success, stated as a win only if its interval excludes 0."""
    gated = pct(gat["success"]["rate"])
    comp = next(
        (
            c
            for c in boundaries[bid].get("comparisons", [])
            if c.get("name") == "ledger_value"
            and c.get("operator") == op
            and c.get("compaction") == gat.get("compaction")  # the setting `blk` and `gat` share
        ),
        None,
    )
    if comp is None:
        return f"With NemoGate carrying the rule, task success was {gated}."
    if comp["lo"] <= 0 <= comp["hi"]:
        return (
            f"With NemoGate carrying the rule, task success was {gated}, {pp(comp['diff'])} "
            f"against blocking alone: {indistinct(comp, boundaries[bid].get('cells', []))}."
        )
    return (
        f"With NemoGate carrying the rule, task success was {gated}, "
        f"{pp(comp['diff'])} against blocking alone (95% interval {pp(comp['lo'])} to "
        f"{pp(comp['hi'])})."
    )


def summaries(boundaries: dict[str, Any]) -> list[str]:
    """One sentence per (boundary, operator) where mutated CVR, block-only success and gated
    success all exist. Nothing is estimated: a missing number means no sentence."""
    out = []
    for bid in sorted(boundaries):
        cells = boundaries[bid].get("cells", [])
        parsed = parse_boundary(bid)
        # only a delegation's recipient is the one acting on the rule; a return may be graded on
        # the sender's reply, so every other kind uses the passive form
        recipient = parsed[2] if parsed and parsed[0] == "delegation" else None
        several = len(compaction_settings(cells)) > 1
        # one sentence per mutated cell, and its block-only and gated cells (and the gate-effect
        # comparison) are the ones made under the same compaction setting
        for mut in (c for c in cells if c["arm"] == "mutated"):
            op, setting = mut["operator"], mut.get("compaction")
            blk = find_cell(cells, "block_only", op, setting)
            gat = find_cell(cells, "gated", op, setting)
            if not (blk and gat):
                continue
            phrase = OPERATOR_PHRASE.get(op, f"altered by {op}")
            broke = f"the {e(recipient)} broke it" if recipient else "the rule was broken"
            under = f" under {e(compaction_note(setting))}" if several else ""
            out.append(
                f"With the rule {e(phrase)} at the handoff <code>{e(bid)}</code>{under}, {broke} "
                f"{pct(mut['cvr']['rate'])} of the time "
                f"({e(mut['violations'])} of {e(mut['n'])} runs). "
                f"Blocking alone left task success at {pct(blk['success']['rate'])}. "
                f"{e(gate_effect(boundaries, bid, op, blk, gat))}"
            )
    return out


# --- cost, time and blocking per arm ---------------------------------------------------------

ATTEMPT_HEAD = "Attempted the rule's action"
BLOCKING_ARMS = ("block_only", "gated")  # the arms whose gate refuses actions


def fixed(x: float, digits: int) -> str:
    """x to `digits` decimals, halves rounded up (the page's convention, as `pct_num`)."""
    scale = 10**digits
    return f"{math.floor(x * scale + 0.5 + 1e-9) / scale:.{digits}f}"


def money(x: float) -> str:
    return f"${fixed(x, 3)}"


def one_decimal(x: float) -> str:
    return fixed(x, 1)


def whole(x: float) -> str:
    return fixed(x, 0)


BOOTSTRAP_FROM = 5  # replay.intervals.MIN_FOR_BOOTSTRAP; used for a cell written without a basis


def mean_text(st: dict[str, Any], fmt: Any, unit: str = "", label: str = "") -> str:
    """ "mean unit label (lo to hi unit)"; no interval for a single run. The interval is a 95%
    bootstrap one from five runs; with fewer it is the lowest to the highest value, said to be a
    range (a bootstrap of two or three values gives just that and would claim far more)."""
    lead = f"{e(fmt(st['mean']))}{e(unit)}"
    if label:
        lead += f" {e(label)}"
    if st.get("lo") is None:
        return f'<span class="rate">{lead}</span>'
    basis = st.get("basis") or ("bootstrap" if st["n"] >= BOOTSTRAP_FROM else "range")
    span = f"{e(fmt(st['lo']))} to {e(fmt(st['hi']))}{e(unit)}"
    if basis == "range":
        interval = f"(range {span})"
        title = "Lowest to highest of the runs; too few runs for an interval"
    else:
        interval = f"({span})"
        title = "95% bootstrap interval"
    return f'<span class="rate">{lead}</span> <span class="ci" title="{e(title)}">{interval}</span>'


def runs_note(n: int) -> str:
    return f"mean of {e(n)} runs" if n > 1 else "1 run, no interval"


def line(text: str) -> str:
    return f'<span class="count">{text}</span>'


def fork_note(cell: dict[str, Any], what: str) -> str:
    """A fork's cost and time cover only what came after the fork point (replay/README.md, "A
    fork record is partial"): say so under any mean that includes one. `n_forks` is left out of
    a cell without forks and of a results file from before it, which get no note."""
    forks = cell.get("n_forks") or 0
    if not forks:
        return ""
    if forks >= cell["n"]:
        return line("all forks: after the fork point only")
    return line(
        f"{e(forks)} of {e(cell['n'])} runs are forks: their {what} is after the fork point only"
    )


def cost_td(cell: dict[str, Any]) -> str:
    cost = cell["arm_metrics"]["cost"]
    return mean_text(cost, money) + line(runs_note(cost["n"])) + fork_note(cell, "cost")


def wall_td(cell: dict[str, Any]) -> str:
    wall = cell["arm_metrics"]["wall_seconds"]
    if not wall["n"]:
        return '<span class="notrun">unknown</span>' + line("no timing recorded")
    note = runs_note(wall["n"])
    if wall.get("unknown"):
        note += f"; {e(wall['unknown'])} not timed"
    return mean_text(wall, whole, " s") + line(note) + fork_note(cell, "time")


def attempt_td(m: dict[str, Any]) -> str:
    att = m.get("attempt_rate")
    if att is None:
        why = "not about a tool or path" if m.get("blocks") else "no trace to count from"
        return f'<span class="notrun">{e(why)}</span>'
    return (
        f'<span class="rate">{pct(att["rate"])}</span> '
        '<span class="ci" title="95% Wilson interval">'
        f"({pct_num(att['lo'])}&ndash;{pct_num(att['hi'])}%)</span>"
        + line(f"{e(att['k'])} of {e(att['n'])} runs, blocked or not")
    )


def after_block_td(arm: str, cell: dict[str, Any]) -> str:
    m = cell["arm_metrics"]
    if arm not in BLOCKING_ARMS:
        return '<span class="notrun">no gate in this arm, nothing is blocked</span>'
    b = m.get("blocks")
    if b is None:
        return '<span class="notrun">no trace to count from</span>'
    parts = [mean_text(b["blocked"], one_decimal, label="blocked calls per run")]
    parts.append(line(mean_text(b["repeats"], one_decimal, label="repeated after a block per run")))
    if b.get("carry_over") and b["carry_over"]["n"]:
        label = "carried into a later delegation per run"
        parts.append(line(mean_text(b["carry_over"], one_decimal, label=label)))
    if b.get("replan"):
        r = b["replan"]
        parts.append(
            line(f"re-planned the same action in {e(r['k'])} of {e(r['n'])} next delegations")
        )
    if b.get("n_ambiguous"):
        k = b["n_ambiguous"]
        parts.append(
            line(
                f"carry-over and re-plans leave out {e(k)} {'run' if k == 1 else 'runs'} with "
                "several task calls at once"
            )
        )
    if b.get("stuck"):
        st = b["stuck"]
        parts.append(line(f"stuck: {e(st['k'])} of {e(st['n'])} blocked runs failed the task"))
    else:
        parts.append(line("no run was blocked"))
    if b.get("recovery"):
        rec = b["recovery"]
        shares = []
        if rec["cost_usd_est"]["mean"] is not None:
            shares.append(f"about {money(rec['cost_usd_est']['mean'])}")
        if rec["seconds_est"]["mean"] is not None:
            shares.append(f"{whole(rec['seconds_est']['mean'])} s")
        extra = f" ({e(' and '.join(shares))}, shared out by step)" if shares else ""
        steps = f"{round(rec['steps']['mean'], 1):g}"
        parts.append(line(f"recovery: {e(steps)} steps after the first block{extra}"))
        if shares:
            parts.append(fork_note(cell, "share of cost and time"))
    return "".join(parts)


def arm_metrics_row(arm: str, op: str, cell: dict[str, Any] | None, note: str = "") -> str:
    head = (
        f'{e(ARM_LABEL.get(arm, arm))}</th><td data-label="Operator">{e(op)}'
        + (f'<span class="count">{e(note)}</span>' if note else "")
        + "</td>"
    )
    if cell is None:
        return (
            f'<tr class="missing"><th scope="row">{head}'
            '<td colspan="4" class="notrun">not run</td></tr>'
        )
    m = cell.get("arm_metrics")
    if not m:
        return (
            f'<tr><th scope="row">{head}'
            '<td colspan="4" class="notrun">not in this results file</td></tr>'
        )
    return (
        f'<tr><th scope="row">{head}<td data-label="Cost per run">{cost_td(cell)}</td>'
        f'<td data-label="Wall time per run">{wall_td(cell)}</td>'
        f'<td data-label="{e(ATTEMPT_HEAD)}">{attempt_td(m)}</td>'
        f'<td data-label="After a block">{after_block_td(arm, cell)}</td></tr>'
    )


def render_arm_metrics(cid: str, bid: str, cells: list[dict[str, Any]]) -> str:
    """Cost, wall time, attempts and what happened after a block, per arm and operator, with one
    block of rows per compaction setting like the table above it.
    Empty when no cell has `arm_metrics` (a results file from before format_version 2)."""
    if not any(c.get("arm_metrics") for c in cells):
        return ""
    settings = compaction_settings(cells)
    split = len(settings) > 1
    rows = "".join(
        arm_metrics_row(
            arm,
            op,
            find_cell(here, arm, op),
            compaction_note(setting) if split or (setting and setting.get("tokens")) else "",
        )
        for setting in settings or [None]
        for here in [[c for c in cells if c.get("compaction") == setting]]
        for arm, op in expected_rows(here)
    )
    table = (
        '<table class="cells arms"><colgroup><col style="width:14%"><col style="width:10%">'
        '<col style="width:18%"><col style="width:16%"><col style="width:16%">'
        '<col style="width:26%"></colgroup><thead><tr>'
        '<th scope="col">Arm</th><th scope="col">Operator</th>'
        '<th scope="col">Cost per run</th><th scope="col">Wall time per run</th>'
        f'<th scope="col">{e(ATTEMPT_HEAD)}</th>'
        '<th scope="col">After a block</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )
    return (
        '<p class="note">Cost and time per run, with a 95% bootstrap interval from five runs and '
        "the lowest to highest value (a range, not an interval) from fewer. Means cover graded "
        "runs only; the money spent on errored runs, per arm, is not in them and is listed "
        "under Money in no rate at the top. Attempted counts calls at the forbidden action "
        "whether or not the gate let them through, so a blocked call is an attempt that failed, "
        "not one that never happened.</p>"
        + panels.tablewrap(f"Cost, time and blocking of {cid} at {bid}", table)
    )


def graded_section(
    cid: str,
    bid: str,
    b: dict[str, Any],
    anchors: Anchors,
    trace: panels.TraceEvidence | None,
) -> str:
    cells = b.get("cells", [])
    # One block of rows per compaction setting, so runs with the summariser on and off are
    # never shown as one row. A single setting leaves the table as it always was.
    settings = compaction_settings(cells)
    split = len(settings) > 1
    rows = "".join(
        cell_row(
            arm,
            op,
            find_cell(here, arm, op),
            compaction_note(setting) if split or (setting and setting.get("tokens")) else "",
        )
        for setting in settings or [None]
        for here in [[c for c in cells if c.get("compaction") == setting]]
        for arm, op in expected_rows(here)
    )
    settings_note = (
        '<p class="note">These runs were made under different compaction settings, so each '
        "setting has its own rows and its own effect comparisons.</p>"
        if split
        else ""
    )
    sid = e(anchors.section(cid, bid))
    table = (
        '<table class="cells"><colgroup><col style="width:21%"><col style="width:13%">'
        '<col style="width:18%"><col style="width:24%"><col style="width:24%"></colgroup>'
        "<thead><tr>"
        '<th scope="col">Arm</th><th scope="col">Operator</th><th scope="col">Runs</th>'
        '<th scope="col">Violation rate</th><th scope="col">Task success</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )
    exemplar = (
        panels.render_trace_panel(trace.exemplars.get((cid, bid)), trace)
        if trace is not None
        else ""
    )
    return (
        f'<section class="bnd" id="{sid}" aria-labelledby="{sid}-h">'
        f'<h4 id="{sid}-h"><code>{e(bid)}</code></h4>'
        + panels.tablewrap(f"Runs of {cid} at {bid}", table)
        + settings_note
        + render_arm_metrics(cid, bid, cells)
        + (
            render_comparisons_by_setting(cells, b.get("comparisons", []), settings)
            if split
            else render_comparisons(cells, b.get("comparisons", []))
        )
        + exemplar
        + "</section>"
    )


def ungraded_section(cid: str, bid: str, b: dict[str, Any], anchors: Anchors) -> str:
    """A boundary of a rule with no compiled check: the runs are counted, nothing is rated."""
    sid = e(anchors.section(cid, bid))
    runs = sum(c["n"] for c in b.get("cells", []))
    return (
        f'<section class="bnd" id="{sid}" aria-labelledby="{sid}-h">'
        f'<h4 id="{sid}-h"><code>{e(bid)}</code></h4>'
        '<p class="note"><span class="notrun">Not graded.</span> This rule has no compiled check, '
        f"so no run can be marked as breaking it. {e(runs)} runs reached this handoff; no "
        "violation rate, success rate or comparison is shown.</p></section>"
    )


def render_constraint(
    cid: str,
    body: dict[str, Any],
    anchors: Anchors,
    trace: panels.TraceEvidence | None = None,
) -> str:
    graded = is_graded(body)
    badge = (
        '<span class="badge yes" title="This rule has a compiled check, so runs can be graded">'
        "Graded</span>"
        if graded
        else '<span class="badge no" title="This rule has no compiled check, so no run can be '
        'graded against it">Not graded</span>'
    )
    summary = (
        "".join(f'<p class="summary">{s}</p>' for s in summaries(body.get("boundaries", {})))
        if graded
        else ""
    )
    tasks = body.get("task_ids") or []
    task_line = (
        f'<p class="note">Tasks that contributed runs: {", ".join(e(t) for t in tasks)}.</p>'
        if tasks
        else ""
    )
    boundaries = body.get("boundaries", {})
    sections = [
        graded_section(cid, bid, boundaries[bid], anchors, trace)
        if graded
        else ungraded_section(cid, bid, boundaries[bid], anchors)
        for bid in sorted(boundaries)
    ]
    return (
        f'<article class="constraint" id="{e(anchors.constraint[cid])}" '
        f'aria-labelledby="{e(anchors.constraint[cid])}-h">'
        f'<header><h3 id="{e(anchors.constraint[cid])}-h"><code>{e(cid)}</code></h3>'
        f'<span class="badge">{e(body.get("kind", "unknown kind"))}</span>{badge}</header>'
        f'<p class="rule">{e(body.get("text", ""))}</p>{summary}{task_line}{"".join(sections)}'
        "</article>"
    )


def render_method(results: dict[str, Any]) -> str:
    """The footer: one plain line always visible, the statistics behind it folded away."""
    terms = (
        ("Intervals", "Every rate comes with a 95% Wilson score interval."),
        (
            "Differences",
            "A difference between two rates uses the Newcombe hybrid-score interval. When it "
            "includes 0 the page says the difference is not distinguishable from 0.",
        ),
        (
            "Pooled",
            "Runs are pooled into a cell (arm, operator, rule, handoff) only within one "
            "reproducibility group: runs that share model ids, safe-config hash, ledger hash, "
            "repo commit and lockfile hash. Other runs are counted as not comparable.",
        ),
        (
            "Clustered",
            "Forked runs share a source run. They are counted, not deduplicated, so a cell that "
            "contains forks is marked clustered and its intervals are too narrow.",
        ),
        (
            "Ungraded rules",
            "A rule with no compiled check is never rated; its runs are only counted.",
        ),
    )
    items = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in terms)
    return (
        '<footer><p class="note">Ranges show how uncertain each number is: the fewer runs behind '
        "it, the wider the range. Whether a rule was obeyed comes from deterministic checks, "
        "never from a model.</p>"
        f'<details class="method"><summary>Method</summary><dl class="kv stack">{items}</dl>'
        f'<p class="note">Format version {e(results["format_version"])}.</p></details></footer>'
    )


# --- page -------------------------------------------------------------------------------------

CSS = (Path(__file__).parent / "site.css").read_text(encoding="utf-8")


def render(
    results: dict[str, Any],
    *,
    trace: panels.TraceEvidence | None = None,
    repairs: list[panels.RepairView] | None = None,
    ledger: Ledger | None = None,
) -> str:
    """The page. `trace` (from --runs), `repairs` (--repair) and `ledger` (--ledger) are optional
    evidence; without `trace` the constraint cards have no trace panel, without `repairs` the
    patch panel says there is no repair run yet."""
    check_format(results)
    constraints = results["constraints"]
    anchors = Anchors(constraints)
    boundaries = collect_boundaries(constraints)
    graph = render_graph(boundaries, anchors)
    if constraints:
        graph_section = (
            '<section aria-labelledby="graph-h"><h2 id="graph-h">Boundary graph</h2>'
            '<p class="note scrollhint">This graph is wider than the screen: scroll sideways.</p>'
            '<div class="graphwrap" tabindex="0" role="region" '
            'aria-label="Boundary graph, scrolls sideways on narrow screens">'
            f"{graph}</div>{render_legend(boundaries)}"
            f"{render_boundary_table(boundaries, anchors)}</section>"
            '<section aria-labelledby="cards-h"><h2 id="cards-h">Constraints</h2>'
            + "".join(
                render_constraint(cid, constraints[cid], anchors, trace)
                for cid in sorted(constraints)
            )
            + "</section>"
        )
    else:
        graph_section = (
            '<div class="empty"><strong>No runs were counted.</strong> There is nothing to chart '
            "yet: no boundary, no constraint and no rate. Nothing here is a zero.</div>"
        )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        "<title>NemoGate: rule survival across handoffs</title>"
        f"<style>{CSS}</style></head><body><main>"
        f"{render_header(results, render_verdict(results, anchors))}"
        f"{panels.render_coverage(results, ledger)}{graph_section}"
        f"{panels.render_patch_panel(repairs)}{render_method(results)}"
        "</main></body></html>\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render results.json as one static HTML page.")
    parser.add_argument(
        "--results", type=Path, required=True, help="results.json from replay.stats"
    )
    parser.add_argument("--out", type=Path, required=True, help="where to write index.html")
    parser.add_argument(
        "--runs",
        type=Path,
        nargs="+",
        metavar="DIR",
        help="run folders, or folders of them: adds the trace panel at the violating action",
    )
    parser.add_argument(
        "--repair",
        type=Path,
        metavar="DIR",
        help="the folder python -m repair wrote: adds the patch",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        help="the ledger the runs used: lists unchecked and unexercised rules",
    )
    args = parser.parse_args(argv)
    try:
        results = json.loads(args.results.read_text(encoding="utf-8"))
        page = render(
            results,
            trace=panels.load_trace_evidence(args.runs, results.get("reproducibility"))
            if args.runs
            else None,
            repairs=panels.load_repairs(args.repair) if args.repair else None,
            ledger=load_ledger_yaml(args.ledger.read_text(encoding="utf-8"))
            if args.ledger
            else None,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(page, encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"report.site: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
