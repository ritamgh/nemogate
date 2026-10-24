"""The results page: one self-contained static HTML file made from `results.json`.

`uv run python -m report.site --results results/results.json --out results/site/index.html
[--runs DIR...] [--repair DIR] [--recall FILE...] [--ledger nemogate.yaml] [--notes FILE]
[--appendix TITLE RESULTS_FILE [NOTES_FILE]]...`

Reads the file `replay/stats.py` writes (format_version 2, nested constraint -> boundary -> cells)
and renders, in order: a short reading guide, "What happened, rule by rule" (per rule, each
cell's runs as done / rule kept, task not finished / broke the rule, and block-only beside
gated after a block), the largest-effect sentence, the key, the honesty strip and header
(reproducibility, run accounting), coverage, a boundary graph (inline SVG, built only from the
boundary ids in the data) and one card per constraint with every rate and interval. The
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
from dataclasses import dataclass
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
        "the gate refuses an action that breaks the rule, without sending the rule again. The "
        "handoff is sabotaged too unless the operator is none.",
    ),
    (
        "Gated",
        "the gate both refuses a rule-breaking action and puts the rule back into the handoff. "
        "The handoff is sabotaged too unless the operator is none.",
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

    def __init__(self, constraints: dict[str, Any], prefix: str = "") -> None:
        """`prefix` keeps an appendix's ids apart from the main page's."""
        self.constraint = {
            cid: f"{prefix}c{i}-{slug(cid)}" for i, cid in enumerate(sorted(constraints))
        }
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


def rule_groups(results: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """Each rule's own `reproducibility` block, None for a results file that has none (written
    before runs were pooled per rule: its one group is the top-level block)."""
    groups = {
        cid: body["reproducibility"]
        for cid, body in results.get("constraints", {}).items()
        if body.get("reproducibility")
    }
    return groups or None


def commits_note(results: dict[str, Any]) -> str:
    """When rules were pooled at different repo commits: every commit, with its rules."""
    by_sha: dict[str, list[str]] = {}
    for cid, group in sorted((rule_groups(results) or {}).items()):
        by_sha.setdefault(group.get("repo_sha") or "none recorded", []).append(cid)
    if len(by_sha) < 2:
        return ""
    listed = ", ".join(f"{short(sha)} ({e(', '.join(cids))})" for sha, cids in by_sha.items())
    return (
        f'<p class="note">This page pools runs made at {len(by_sha)} repo commits; each rule\'s '
        f"numbers are at its own commit, named in its section: {listed}.</p>"
    )


def render_header(results: dict[str, Any], recalls: Sequence[panels.RecallView] = ()) -> str:
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
            ("repo_sha", "repo commit, largest group" if commits_note(results) else "repo commit"),
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
    files = list(recalls) if recall is None else []
    recall_item = (
        panels.render_recall_items(files)
        if files
        else f"""<li><span class="v">{e(recall_text)}</span>
<span class="k">extraction recall: how many of the seeded rules the extractor found</span></li>"""
    )
    recall_caveat = f'<p class="note">{e(panels.RECALL_CAVEAT)}</p>' if files else ""
    total, with_check = (
        honesty.get("constraints_total", 0),
        honesty.get("constraints_with_check", 0),
    )
    inconclusive_total = honesty.get("inconclusive_total", 0)
    rules = results.get("constraints", {}).values()
    if results["reproducibility"] and any(
        b.get("reproducibility") not in (None, results["reproducibility"]) for b in rules
    ):
        group_note = (
            "Each rule was pooled under the conditions shown in its own section; these are the "
            "conditions of the largest group."
        )
    elif results["reproducibility"]:
        group_note = "Only runs made under exactly these conditions were counted."
    else:
        group_note = "No runs were counted, so there is no shared set of conditions."
    return f"""<section aria-labelledby="honesty-h" class="strip">
<h2 id="honesty-h">What these numbers can and cannot tell you</h2>
<ul class="honesty">
{recall_item}
<li><span class="v">{e(with_check)} of {e(total)}</span>
<span class="k">rules can be graded: each has a compiled check, a program that decides
pass or fail</span></li>
<li><span class="v">{e(inconclusive_total)}</span>
<span class="k">inconclusive runs, left out of every rate below. Natural runs where the
rule never came up are not among them: they stay in the rates, and each natural row counts
them</span></li>
</ul>{recall_caveat}
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
{commits_note(results)}<dl class="kv">{hashes}</dl>
</div>
</section>"""


def render_top(exact: bool = False, glance: str = "") -> str:
    return f"""<header class="top">
<h1>NemoGate results</h1>
<p class="lead">Do rules survive the handoffs between agents? Each rule was given to a team of
agents, with and without sabotaging the handoff and with and without the NemoGate gate, and every
run was checked against it.</p>
{glance}{render_howto(exact)}
</header>"""


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
    # The sentence quotes one compaction setting's cells: name it, and say others exist
    several = len(compaction_settings(cells)) > 1
    under = f", under {e(compaction_note(comp.get('compaction')))}" if several else ""
    others = (
        " Other compaction settings were run at this handoff; each has its own rows below."
        if several
        else ""
    )
    return (
        '<p class="verdict-line"><strong>Largest effect found.</strong> '
        f'At <a href="#{e(anchors.section(cid, bid))}"><code>{e(cid)}</code> on '
        f"<code>{e(bid)}</code></a>{under}, with the rule {e(phrase)}, it was broken in "
        f"{e(mut['violations'])} of {e(mut['n'])} runs ({pct(mut['cvr']['rate'])}) against "
        f"{e(nat['violations'])} of {e(nat['n'])} with no sabotage ({pct(nat['cvr']['rate'])}): "
        f"{e(pp(comp['diff']))} (95% interval {e(pp(comp['lo']))} to {e(pp(comp['hi']))}"
        f"{e(tail)}). {gated}{others}</p>"
    )


def render_key() -> str:
    arms = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in KEY_ARMS)
    ops = "".join(
        f"<div><dt><code>{e(k)}</code></dt><dd>{e(v)}</dd></div>" for k, v in KEY_OPERATORS
    )
    terms = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in TERMS)
    return (
        '<section aria-labelledby="key-h" class="key"><h2 id="key-h">Key</h2><div class="keycols">'
        f'<div><h3>Arms (how a run was set up)</h3><dl class="kv stack">{arms}</dl></div>'
        f'<div><h3>Operators (how the handoff is sabotaged)</h3><dl class="kv stack">{ops}</dl>'
        f'<h3>Words on this page</h3><dl class="kv stack terms">{terms}</dl>'
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


PLACED = ("CARRIED_OBEYED", "DISOBEYED", "DROPPED_LUCKY", "DROPPED")
NOT_PLACED = "Where the rule went in the handoff is not recorded for these runs."


def presence_grid(cell: dict[str, Any]) -> str:
    """For a cell with `labels` (operator none): was the rule in the handoff, and was it kept.

    The carriage label answers both at once: carried or dropped (present or lost) and kept or
    broken. Runs the classifier could not place (the rule never came up, no verdict, no
    carriage.json) are counted below the grid, so the grid and the note add up to the cell's n.
    Empty when the field is absent (older results files); `cell_row` then keeps its older notes.
    When no run is placed the grid is left out for one line saying so.
    """
    labels = cell["labels"]
    if not any(labels.get(name, 0) for name in PLACED):
        # a grid of four zeros would read as "measured, and none of them"
        return f'<span class="count">{e(NOT_PLACED)}</span>'

    def count(name: str) -> str:
        return f'<span role="cell">{e(labels.get(name, 0))}</span>'

    unclear = sum(v for k, v in labels.items() if k not in (*PLACED, "NOT_EXERCISED", "unlabelled"))
    unplaced = (
        (labels.get("NOT_EXERCISED", 0), "where the rule never came up"),
        (unclear, "the classifier could not place"),
        (labels.get("unlabelled", 0), "with no carriage record"),
    )
    parts = [f"{k} {text}" for k, text in unplaced if k]
    foot = f'<span class="count">Not placed: {e(", ".join(parts))}.</span>' if parts else ""
    # block_only and gated count a blocked attempt as "broke" (replay/carriage.py: broke =
    # violated or blocked), but the violation rate beside the grid counts only what got through
    blocking = cell.get("arm") in ("block_only", "gated")
    broke_header = "Agent broke it or tried to" if blocking else "Agent broke it"
    if blocking:
        foot += (
            '<span class="count">Blocked attempts count here; the violation rate counts only '
            "what got through.</span>"
        )
    if cell.get("arm") == "gated":  # the label describes what the receiver got, re-pin included
        foot += "<span class=\"count\">Gated: 'in the handoff' includes the gate's re-pin.</span>"
    return (
        '<span class="presence" role="table" aria-label="Rule in the handoff against '
        'whether the agent kept it">'
        '<span role="row"><span role="presentation"></span>'
        '<span role="columnheader">Agent kept it</span>'
        f'<span role="columnheader">{broke_header}</span></span>'
        '<span role="row"><span role="rowheader">Rule in the handoff</span>'
        f"{count('CARRIED_OBEYED')}{count('DISOBEYED')}</span>"
        '<span role="row"><span role="rowheader">Rule lost in the handoff</span>'
        f"{count('DROPPED_LUCKY')}{count('DROPPED')}</span></span>" + foot
    )


OUTCOMES_NOTE = (
    "Rule kept, task not finished: the rule was obeyed but the task was not finished, for "
    "example a deletion that waited for the user's sign-off. This is the cost of complying, "
    "not a failure to comply."
)


def outcomes_split(cell: dict[str, Any]) -> str:
    """For a cell with `outcomes`: its runs as done, rule kept but task not finished, or broke the
    rule. Each run is in one of the three (a run that broke the rule counts as broke even when
    the task finished), so they add up to the cell's runs. Empty for older results files."""
    outcomes = cell.get("outcomes")
    if not outcomes:
        return ""
    return (
        '<span class="count outcomes" title="A run that broke the rule counts only as broke, '
        'even if the task finished.">'
        f"done {e(outcomes['done'])} &middot; "
        f"rule kept, task not finished {e(outcomes['kept_open'])} &middot; "
        f"broke the rule {e(outcomes['broke'])}</span>"
    )


def cell_row(
    arm: str, op: str, cell: dict[str, Any] | None, note: str = "", attempts: bool = False
) -> str:
    label = (
        f'{e(ARM_LABEL.get(arm, arm))}</th><td data-label="Operator">{e(op)}'
        + (f'<span class="count">{e(note)}</span>' if note else "")
        + "</td>"
    )
    if cell is None:
        return (
            f'<tr class="missing"><th scope="row">{label}'
            f'<td colspan="{4 if attempts else 3}" class="notrun">not run</td></tr>'
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
    if op == "none" and "labels" in cell:
        # `labels` counts every run, so it holds the numbers of both older natural notes
        cvr += presence_grid(cell)
    elif cell.get("arm") == "natural":
        cvr += split_note(cell) + unexercised_note(cell)
    success = fraction_cell(cell["successes"], n, cell["success"]) + outcomes_split(cell)
    tried = (
        f'<td data-label="{e(ATTEMPT_HEAD)}">{attempt_td(cell.get("arm_metrics"))}</td>'
        if attempts
        else ""
    )
    return (
        f'<tr><th scope="row">{label}<td data-label="Runs">{n_text}{warn}</td>'
        f'<td data-label="Violation rate">{cvr}</td>'
        f'<td data-label="{e(PASSED)}">{success}</td>{tried}</tr>'
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
        "Gate effect on passing the task",
        "share of runs that passed the task with the rule carried (gated) minus with blocking "
        "alone; the rule is not considered, so a run that broke it and passed counts as passed",
        "gated",
    ),
    (
        "attempt_reduction",
        "Gate effect on attempts",
        "attempt rate with the rule carried (gated) minus attempt rate with blocking alone; "
        "below 0 means the coder tried the forbidden action less often",
        "gated",
    ),
)
OPTIONAL_COMPARISONS = ("attempt_reduction",)  # left out, not shown "not run", when absent


def n_text(*counts: int) -> str:
    """ "n = 10" when the groups compared are the same size, else "n = 10 and 8"."""
    return "n = " + " and ".join(dict.fromkeys(str(c) for c in counts))


def compared_n(cells: list[dict[str, Any]], comp: dict[str, Any]) -> list[int]:
    """The run counts of the two cells a comparison is made from (empty if a cell is missing)."""
    if "n_a" in comp:  # an attempt comparison counts the runs that have a trace
        return [comp["n_a"], comp["n_b"]]
    knobs = comp.get("compaction", ANY)
    pair = [find_cell(cells, comp[side]["arm"], comp[side]["operator"], knobs) for side in "ab"]
    return [c["n"] for c in pair if c]


def indistinct(comp: dict[str, Any], cells: list[dict[str, Any]]) -> str:
    """Plain wording for an interval that includes 0, with the run counts it rests on."""
    counts = compared_n(cells, comp)
    return "not distinguishable from 0 at " + (n_text(*counts) if counts else "these sample sizes")


def broke_note(cells: list[dict[str, Any]], comp: dict[str, Any]) -> str:
    """Under the gate effect on passing the task: whether the two arms' passes all kept the rule
    (then it is also the difference in clean successes). Empty without `outcomes`."""
    knobs = comp.get("compaction", ANY)
    pair = [find_cell(cells, comp[side]["arm"], comp[side]["operator"], knobs) for side in "ab"]
    if not all(c and c.get("outcomes") for c in pair):
        return ""
    if all(c["outcomes"]["broke"] == 0 for c in pair if c):
        return line(
            "Neither arm broke the rule here, so this is also the difference in runs that did the "
            "job without breaking the rule."
        )
    return line(
        "Some of these runs broke the rule; a run that broke it and passed the task counts as "
        "passed here."
    )


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
            if comp is None and name in OPTIONAL_COMPARISONS:
                continue  # an older results file, or a cell with nothing to count from
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
                    + (broke_note(cells, comp) if name == "ledger_value" else "")
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
                f"Blocking alone left task success (rule not considered) at "
                f"{pct(blk['success']['rate'])}. "
                f"{e(gate_effect(boundaries, bid, op, blk, gat))}"
            )
    return out


# --- cost, time and blocking per arm ---------------------------------------------------------

ATTEMPT_HEAD = "Attempted the rule's action"
ATTEMPT_NOTE = (
    "Attempted the rule's action: runs with at least one call at the forbidden action, whether "
    "or not the gate let it through. The violation rate counts only what got through, so where "
    "the gate blocks the action a blocking arm can show no violations while the coder still "
    "tried; this column is what tells block_only and gated apart."
)
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


def attempt_td(m: dict[str, Any] | None) -> str:
    if not m:
        return '<span class="notrun">not in this results file</span>'
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
            '<td colspan="3" class="notrun">not run</td></tr>'
        )
    m = cell.get("arm_metrics")
    if not m:
        return (
            f'<tr><th scope="row">{head}'
            '<td colspan="3" class="notrun">not in this results file</td></tr>'
        )
    return (
        f'<tr><th scope="row">{head}<td data-label="Cost per run">{cost_td(cell)}</td>'
        f'<td data-label="Wall time per run">{wall_td(cell)}</td>'
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
        '<col style="width:22%"><col style="width:20%">'
        '<col style="width:34%"></colgroup><thead><tr>'
        '<th scope="col">Arm</th><th scope="col">Operator</th>'
        '<th scope="col">Cost per run</th><th scope="col">Wall time per run</th>'
        '<th scope="col">After a block</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )
    return (
        '<p class="note">Cost and time per run, with a 95% bootstrap interval from five runs and '
        "the lowest to highest value (a range, not an interval) from fewer. Means cover graded "
        "runs only; the money spent on errored runs, per arm, is not in them and is listed "
        "under Money in no rate at the top.</p>"
        + panels.tablewrap(f"Cost, time and blocking of {cid} at {bid}", table)
    )


def graded_section(
    cid: str,
    bid: str,
    b: dict[str, Any],
    anchors: Anchors,
    trace: panels.TraceEvidence | None,
    detail: bool = True,
) -> str:
    """One boundary of a graded rule. `detail=False` (an appendix) keeps the cell table and its
    notes and leaves out the cost and time table, the effect comparisons and the trace."""
    cells = b.get("cells", [])
    # One block of rows per compaction setting, so runs with the summariser on and off are
    # never shown as one row. A single setting leaves the table as it always was.
    settings = compaction_settings(cells)
    split = len(settings) > 1
    # the attempt column comes with `arm_metrics`: a results file from before it has neither
    any_attempts = any(c.get("arm_metrics") for c in cells)
    rows = "".join(
        cell_row(
            arm,
            op,
            find_cell(here, arm, op),
            compaction_note(setting) if split or (setting and setting.get("tokens")) else "",
            any_attempts,
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
    any_outcomes = any(c.get("outcomes") for c in cells)
    sid = e(anchors.section(cid, bid))
    if any_attempts:
        widths = (17, 11, 12, 20, 20, 20)
        extra_head = f'<th scope="col">{e(ATTEMPT_HEAD)}</th>'
    else:
        widths = (21, 13, 18, 24, 24)
        extra_head = ""
    cols = "".join(f'<col style="width:{w}%">' for w in widths)
    table = (
        f'<table class="cells"><colgroup>{cols}</colgroup>'
        "<thead><tr>"
        '<th scope="col">Arm</th><th scope="col">Operator</th><th scope="col">Runs</th>'
        '<th scope="col">Violation rate</th>'
        f'<th scope="col">{e(PASSED)}</th>'
        f"{extra_head}</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    exemplar = (
        panels.render_trace_panel(trace.exemplars.get((cid, bid)), trace)
        if trace is not None
        else ""
    )
    more = ""
    if detail:
        more = (
            render_arm_metrics(cid, bid, cells)
            + (
                render_comparisons_by_setting(cells, b.get("comparisons", []), settings)
                if split
                else render_comparisons(cells, b.get("comparisons", []))
            )
            + exemplar
        )
    return (
        f'<section class="bnd" id="{sid}" aria-labelledby="{sid}-h">'
        f'<h4 id="{sid}-h"><code>{e(bid)}</code></h4>'
        + panels.tablewrap(f"Runs of {cid} at {bid}", table)
        + (f'<p class="note outcomes-note">{e(OUTCOMES_NOTE)}</p>' if any_outcomes else "")
        + (f'<p class="note attempts-note">{e(ATTEMPT_NOTE)}</p>' if any_attempts else "")
        + settings_note
        + more
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
    pooled = body.get("reproducibility")
    if pooled:
        set_aside = body.get("not_comparable", 0)
        task_line += (
            '<p class="note">Pooled at repo commit '
            f"{short(pooled.get('repo_sha') or 'none recorded')}"
            + (
                f"; {e(set_aside)} runs of this rule under other conditions were set aside as "
                "not comparable"
                if set_aside
                else ""
            )
            + ".</p>"
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


# --- what happened: the outcome section --------------------------------------------------------

HEADLINE = "Did the job without breaking the rule"
PASSED = "Task passed (rule not considered)"
KEPT_OPEN = "Rule kept, task not finished"
OP_ROW = {  # how a row names its operator
    "none": "no sabotage",
    "drop": "rule lost (drop)",
    "hedge": "rule made vague (hedge)",
    "compress_100": "handoff compressed to 100 words (compress_100)",
}
ARM_WORD = {
    "natural": "natural",
    "mutated": "mutated",
    "block_only": "block-only",
    "gated": "gated",
}
HOWTO = (
    "Each run either <strong>did the job without breaking the rule</strong>, "
    "<strong>kept the rule without finishing the task</strong>, or "
    "<strong>broke the rule</strong>. "
    "The first is the result that counts. The gate stops the rule from being broken. Finishing "
    "the task after a block needs a route that keeps the rule or a human who can approve; that "
    "depends on the agent system around the gate, so an unfinished task with the rule kept is "
    "not a gate failure.",
    "&ldquo;Task passed&rdquo; alone ignores the rule: a run that broke the rule can still pass "
    "the task's own tests and check.",
    "Natural adds nothing. Mutated sabotages the handoff (the operator says how; "
    "&ldquo;no sabotage&rdquo; means none). Block-only refuses a rule-breaking call. Gated "
    "(NemoGate) refuses it and also puts the rule back into the handoff.",
    "Every share comes with its run count. The exact rates and their 95% intervals are in each "
    "rule's details further down; the words are defined in the Key.",
)
TERMS = (
    (
        "Boundary",
        "a handoff between two agents, where the context is shortened and passed on: "
        "delegation:<from>-><to> hands work down, return:<from>-><to> hands the answer back.",
    ),
    (
        "Compaction",
        "the planner's conversation is summarised once it grows past a token limit. Runs with it "
        "on and off are counted apart.",
    ),
    (
        "Not comparable",
        "a run made under other conditions (models, safe config, ledger, repo commit or "
        "lockfile) than the runs its rule is counted with. It is left out of the rates.",
    ),
    (
        "Inconclusive",
        "a run that gives no verdict: it errored, its record could not be read, it was not "
        "comparable, or the sabotage changed nothing. It is left out of every rate.",
    ),
    (
        "95% interval",
        "the range the true rate is likely in, given how many runs there are: fewer runs, wider "
        "range. How it is computed is under Method at the foot of the page.",
    ),
)


def op_row(op: str) -> str:
    return OP_ROW.get(op, f"altered by {op}")


def passed_anyway(cell: dict[str, Any]) -> int | None:
    """Runs that broke the rule and still passed the task: task successes minus the runs that
    were done (no violation and the task passed). None without `outcomes`, or if the counts do
    not fit together (then nothing is claimed)."""
    out = cell.get("outcomes")
    if not out:
        return None
    k = cell["successes"] - out["done"]
    return k if 0 <= k <= out["broke"] else None


def stopped_by_block(cell: dict[str, Any]) -> str:
    """For a cell whose arm blocks: how many of its runs that kept the rule without finishing the
    task were stopped by a block, from the block counts only. `stuck` counts runs with a block
    that failed the task, and a run that broke the rule by another call can be among them, so
    the count is a range when the cell has rule breakers. Empty when the data shows none."""
    out = cell.get("outcomes")
    blocks = (cell.get("arm_metrics") or {}).get("blocks")
    if not out or not blocks or not blocks.get("runs_with_block") or not blocks.get("stuck"):
        return ""
    stuck, unfinished = blocks["stuck"]["k"], out["kept_open"]
    lo, hi = max(0, stuck - out["broke"]), min(stuck, unfinished)
    if hi <= 0 or lo > hi:
        return ""
    count = f"{lo}" if lo == hi else f"{lo} to {hi}"
    return f"{count} of {unfinished} were stopped by a block"


def task_phrase(body: dict[str, Any]) -> str:
    tasks = body.get("task_ids") or []
    if not tasks:
        return ""
    return ("task " if len(tasks) == 1 else "tasks ") + ", ".join(tasks)


def ordered_cells(body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(boundary, cell) for every cell of a rule: by boundary, compaction setting, arm, operator."""
    out = []
    for bid in sorted(body.get("boundaries", {})):
        cells = body["boundaries"][bid].get("cells", [])
        settings = compaction_settings(cells)
        for c in sorted(
            cells,
            key=lambda c: (settings.index(c.get("compaction")), arm_rank(c["arm"]), c["operator"]),
        ):
            out.append((bid, c))
    return out


def row_note(body: dict[str, Any], bid: str, cell: dict[str, Any]) -> str:
    """The boundary when the rule has several, and the compaction setting when it matters."""
    parts = []
    if len(body.get("boundaries", {})) > 1:
        parts.append(bid)
    setting = cell.get("compaction")
    cells = body["boundaries"][bid].get("cells", [])
    if len(compaction_settings(cells)) > 1 or (setting and setting.get("tokens")):
        parts.append(compaction_note(setting))
    return "; ".join(parts)


def outcome_bar(cell: dict[str, Any]) -> str:
    """Three segments, done, rule kept but task not finished, broke, as shares of the cell's
    runs. Decorative: the counts are in the row. Broke is striped and never in the done colour."""
    out, n = cell["outcomes"], cell["n"]
    segs = "".join(
        f'<span class="seg {cls}" style="width:{round(100 * out[key] / n, 2):g}%"></span>'
        for key, cls in (("done", "done"), ("kept_open", "open"), ("broke", "broke"))
        if out[key]
    )
    return f'<span class="obar" aria-hidden="true">{segs}</span>'


def outcome_row(body: dict[str, Any], bid: str, cell: dict[str, Any]) -> str:
    n = cell["n"]
    note = row_note(body, bid, cell)
    head = (
        f'<th scope="row">{e(ARM_LABEL.get(cell["arm"], cell["arm"]))} '
        f'<span class="op">{e(op_row(cell["operator"]))}</span>'
        + (f'<span class="count">{e(note)}</span>' if note else "")
        + "</th>"
    )
    out = cell.get("outcomes")
    if out and n:
        headline = (
            f'<span class="rate">{pct(out["done"] / n)}</span>'
            f'<span class="count">{e(out["done"])} of {e(n)} runs</span>' + outcome_bar(cell)
        )
        kept, broke = e(out["kept_open"]), e(out["broke"])
        stopped = stopped_by_block(cell)
        if stopped:
            kept += f'<span class="count">{e(stopped)}</span>'
        anyway = passed_anyway(cell)
        if anyway:
            headline += (
                '<span class="count">'
                f"Passed the task but broke the rule: {e(anyway)} of {e(n)} runs</span>"
            )
        if not out["broke"]:
            anyway_text = '<span class="notrun">none broke it</span>'
        elif anyway is None:
            anyway_text = "&ndash;"
        else:
            anyway_text = f"{e(anyway)} of {e(out['broke'])}"
    else:
        headline = '<span class="notrun">not in this results file</span>'
        kept, broke, anyway_text = "&ndash;", e(cell["violations"]), "&ndash;"
    return (
        f"<tr>{head}"
        f'<td data-label="Runs">{e(n)}</td>'
        f'<td data-label="{e(HEADLINE)}" class="headline">{headline}</td>'
        f'<td data-label="{e(KEPT_OPEN)}">{kept}</td>'
        f'<td data-label="Broke the rule">{broke}</td>'
        f'<td data-label="Of those, task passed anyway">{anyway_text}</td>'
        f'<td data-label="{e(PASSED)}" class="passed">{e(cell["successes"])} of {e(n)}</td></tr>'
    )


def outcome_table(cid: str, body: dict[str, Any]) -> str:
    rows = "".join(outcome_row(body, bid, c) for bid, c in ordered_cells(body))
    table = (
        '<table class="outcome"><colgroup><col style="width:22%"><col style="width:7%">'
        '<col style="width:24%"><col style="width:12%"><col style="width:10%">'
        '<col style="width:12%"><col style="width:13%"></colgroup><thead><tr>'
        '<th scope="col">Arm and sabotage</th><th scope="col">Runs</th>'
        f'<th scope="col">{e(HEADLINE)}</th>'
        f'<th scope="col">{e(KEPT_OPEN)}</th>'
        '<th scope="col">Broke the rule</th>'
        '<th scope="col">Of those, task passed anyway</th>'
        f'<th scope="col">{e(PASSED)}</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )
    return panels.tablewrap(f"What happened in the runs of {cid}", table)


def block_row(body: dict[str, Any], bid: str, cell: dict[str, Any]) -> str:
    note = row_note(body, bid, cell)
    head = (
        f'<th scope="row">{e(ARM_LABEL.get(cell["arm"], cell["arm"]))} '
        f'<span class="op">{e(op_row(cell["operator"]))}</span>'
        + (f'<span class="count">{e(note)}</span>' if note else "")
        + "</th>"
    )
    m = cell.get("arm_metrics") or {}
    b = m.get("blocks")
    if b is None:
        why = "not in this results file" if not m else "no trace to count from"
        return f'<tr>{head}<td colspan="5" class="notrun">{e(why)}</td></tr>'
    stuck = b.get("stuck")
    stuck_text = (
        f"{e(stuck['k'])} of {e(stuck['n'])}"
        if stuck
        else '<span class="notrun">no run was blocked</span>'
    )
    att = m.get("attempt_rate")
    att_text = (
        f"{e(att['k'])} of {e(att['n'])} ({pct(att['rate'])})"
        if att
        else '<span class="notrun">not about a tool or path</span>'
    )
    return (
        f"<tr>{head}"
        f'<td data-label="Runs with a block">{e(b["runs_with_block"])} of {e(cell["n"])}</td>'
        f'<td data-label="Blocked calls per run">{mean_text(b["blocked"], one_decimal)}</td>'
        f'<td data-label="Retries per run">{mean_text(b["repeats"], one_decimal)}</td>'
        f'<td data-label="Stuck: blocked, then failed the task">{stuck_text}</td>'
        f'<td data-label="Tried the forbidden action">{att_text}</td></tr>'
    )


def attempt_lines(body: dict[str, Any], bid: str) -> list[tuple[dict[str, Any], str]]:
    """The gated minus block-only attempt rate, per operator (and compaction setting), each with
    the comparison it was written from."""
    b = body["boundaries"][bid]
    cells = b.get("cells", [])
    several = len(compaction_settings(cells)) > 1
    out = []
    for comp in b.get("comparisons", []):
        if comp.get("name") != "attempt_reduction":
            continue
        under = f", {compaction_note(comp.get('compaction'))}" if several else ""
        crosses = comp["lo"] <= 0 <= comp["hi"]
        tail = f", {indistinct(comp, cells)}" if crosses else "; the interval excludes 0"
        out.append(
            (
                comp,
                "Gated minus block-only, runs that tried the forbidden action "
                f"({e(comp.get('operator'))}{e(under)}): {e(pp(comp['diff']))} (95% interval "
                f"{e(pp(comp['lo']))} to {e(pp(comp['hi']))}){e(tail)}.",
            )
        )
    return out


# --- exact tests (p-values), from `exact_tests` of a boundary ------------------------------------

EXACT_OUTCOME = {
    "violations": "broke the rule",
    "attempts": "tried the forbidden action",
    "done": "did the job and kept the rule",
}
EXACT_TESTS = ("fisher", "mcnemar")


def p_figure(p: float) -> str:
    """The figure of a p-value: three significant figures, "< 0.001" below that."""
    return "< 0.001" if p < 0.001 else f"{p:.3g}"


def p_text(p: float) -> str:
    """ "p = 0.333" with three significant figures; "p < 0.001" below that."""
    return "p < 0.001" if p < 0.001 else f"p = {p:.3g}"


def sources_text(n: int) -> str:
    return f"{n} source" + ("" if n == 1 else "s")


def source_floor(n_sources: int) -> str:
    """The smallest p the exact test on `n_sources` sources can give: 2 * 0.5^n, at most 1."""
    floor = min(1.0, 2 * 0.5**n_sources)
    return "< 0.001" if floor < 0.001 else f"{floor:.3g}"


EXACT_COLUMNS = (
    "Outcome",
    "Operator",
    "Gated",
    "Block-only",
    "Test",
    "p over runs or pairs",
    "p counting each source once",
)
EXACT_TEST_WORD = {
    "fisher": "Fisher, independent runs",
    "mcnemar": "McNemar, matched forks of the same sources",
}


def exact_sides(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """(side of the entry, prefix) for the Gated and the Block-only column. The entry says which of
    its sides `a` and `b` is which arm. An entry of other arms keeps its own order and names
    each arm in its cell, so nothing is printed under the wrong heading."""
    arm = {side: entry[side]["arm"] for side in "ab"}
    if set(arm.values()) == set(BLOCKING_ARMS) and arm["a"] != arm["b"]:
        side_of = {name: side for side, name in arm.items()}
        return [(side_of["gated"], ""), (side_of["block_only"], "")]
    return [(side, f"{ARM_WORD.get(arm[side], arm[side])}: ") for side in "ab"]


def exact_count(entry: dict[str, Any], side: str, prefix: str) -> str:
    """One arm's count: runs for independent runs, the pairs only that arm had the outcome in
    for matched forks."""
    if entry["test"] == "fisher":
        text = f"{entry['k_' + side]} of {entry['n_' + side]} runs"
    else:
        text = f"{entry['discordant'][side + '_only']} of {entry['n_pairs']} pairs (only this arm)"
    return e(prefix + text)


def exact_row(entry: dict[str, Any], several: bool) -> str:
    """One table row for an `exact_tests` entry; every number and arm name is the entry's."""
    outcome = EXACT_OUTCOME[entry["name"]]
    op = entry["operator"]
    scope = e({"pooled": "Pooled over operators", "none": "no sabotage"}.get(op, op))
    if several:
        scope += f'<span class="count">{e(compaction_note(entry.get("compaction")))}</span>'
    by_source = entry.get("p_exact_by_source")
    by_source_text = (
        e(f"{p_figure(by_source)} ({sources_text(entry['n_sources'])})")
        if by_source is not None
        else DASH
    )
    gated, block_only = (exact_count(entry, side, prefix) for side, prefix in exact_sides(entry))
    values = (
        scope,
        gated,
        block_only,
        e(EXACT_TEST_WORD[entry["test"]]),
        e(p_figure(entry["p_exact"])),
        by_source_text,
    )
    return (
        f'<tr><th scope="row">{e(outcome[0].upper() + outcome[1:])}</th>'
        + "".join(
            f'<td data-label="{e(label)}">{value}</td>'
            for label, value in zip(EXACT_COLUMNS[1:], values, strict=True)
        )
        + "</tr>"
    )


def exact_footnote(entries: list[dict[str, Any]]) -> str:
    """For the paired entries whose source test dropped no ties: the smallest p each number of
    sources can give, 2 * 0.5^n (at most 1). Empty when there is none."""
    sources = sorted(
        {
            t["n_sources"]
            for t in entries
            if t["test"] == "mcnemar"
            and t.get("p_exact_by_source") is not None
            and t["by_source"]["ties"] == 0
            and t["n_sources"] >= 1
        }
    )
    if not sources:
        return ""
    floors = "; ".join(
        f"with {sources_text(n)} the smallest p possible is {source_floor(n)}" for n in sources
    )
    return f'<p class="note exact">{e(f"Counting each source once, {floors}.")}</p>'


def exact_table(cid: str, bid: str, entries: list[dict[str, Any]], several: bool) -> str:
    """The exact tests of one boundary as one table, a row per entry in the order given."""
    widths = (17, 13, 14, 14, 16, 12, 14)
    cols = "".join(f'<col style="width:{w}%">' for w in widths)
    head = "".join(f'<th scope="col">{e(label)}</th>' for label in EXACT_COLUMNS)
    rows = "".join(exact_row(t, several) for t in entries)
    table = (
        f'<table class="blocks exact"><colgroup>{cols}</colgroup><thead><tr>{head}</tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )
    return panels.tablewrap(f"Exact tests of {cid} at {bid}", table) + exact_footnote(entries)


def exact_entries(b: dict[str, Any]) -> list[dict[str, Any]]:
    """The boundary's entries that the page can print, in page order: by outcome, then the
    compaction setting, then each operator, the pooled entry last."""
    entries = [
        t
        for t in b.get("exact_tests") or []
        if t.get("name") in EXACT_OUTCOME and t.get("test") in EXACT_TESTS
    ]
    settings = compaction_settings([{"compaction": t.get("compaction")} for t in entries])
    return sorted(
        entries,
        key=lambda t: (
            tuple(EXACT_OUTCOME).index(t["name"]),
            settings.index(t.get("compaction")),
            t["operator"] == "pooled",
            t["operator"],
        ),
    )


def boundary_lines(cid: str, body: dict[str, Any], bid: str) -> str:
    """The gated-minus-block-only interval lines of one boundary, as paragraphs, then its exact
    tests as one table (by outcome, the pooled entry last under its operators)."""
    b = body["boundaries"][bid]
    several = len(compaction_settings(b.get("cells", []))) > 1
    entries = exact_entries(b)
    out = [f'<p class="note">{text}</p>' for _, text in attempt_lines(body, bid)]
    if entries:
        out.append(exact_table(cid, bid, entries, several))
    return "".join(out)


def block_table(cid: str, body: dict[str, Any]) -> str:
    """Block-only beside gated after a block, for a rule whose block-only and gated cells carry
    block counts. Empty otherwise (an older results file, or one blocking arm only)."""
    tables = []
    for bid in sorted(body.get("boundaries", {})):
        cells = body["boundaries"][bid].get("cells", [])
        mine = [c for c in cells if c["arm"] in BLOCKING_ARMS]
        if {c["arm"] for c in mine} != set(BLOCKING_ARMS) or not any(
            (c.get("arm_metrics") or {}).get("blocks") for c in mine
        ):
            if exact_entries(body["boundaries"][bid]):
                where = f" at <code>{e(bid)}</code>" if len(body["boundaries"]) > 1 else ""
                tables.append(
                    f"<h4>Block-only against gated, exact tests{where}</h4>"
                    + boundary_lines(cid, body, bid)
                )
            continue
        rows = "".join(
            block_row(body, b, c) for b, c in ordered_cells(body) if b == bid and c in mine
        )
        table = (
            '<table class="blocks"><colgroup><col style="width:22%"><col style="width:13%">'
            '<col style="width:17%"><col style="width:17%"><col style="width:15%">'
            '<col style="width:16%"></colgroup><thead><tr>'
            '<th scope="col">Arm and sabotage</th><th scope="col">Runs with a block</th>'
            '<th scope="col">Blocked calls per run</th><th scope="col">Retries per run</th>'
            '<th scope="col">Stuck: blocked, then failed the task</th>'
            '<th scope="col">Tried the forbidden action</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>"
        )
        lines = boundary_lines(cid, body, bid)
        where = f" at <code>{e(bid)}</code>" if len(body["boundaries"]) > 1 else ""
        tables.append(
            f"<h4>When the gate blocked a call{where}: block-only against gated</h4>"
            + panels.tablewrap(f"Block-only against gated for {cid} at {bid}", table)
            + lines
        )
    return "".join(tables)


BLOCK_NOTE = (
    '<p class="note">A retry is a call on a file or tool the gate had already refused in that '
    "run. Stuck counts the runs with a block that then failed the task. Tried the forbidden "
    "action counts runs with at least one such call, blocked or not: both arms block, so this "
    "is where carrying the rule (gated) can differ from blocking alone. Means come with a 95% "
    "bootstrap interval from five runs, and the lowest to highest value from fewer.</p>"
)


def render_group(cid: str, body: dict[str, Any], anchors: Anchors, explain: bool) -> str:
    """One rule's outcome table and block table. `explain`: add the block table's definitions
    (the first block table on the page only)."""
    meta = [f"rule <code>{e(cid)}</code>"]
    if task_phrase(body):
        phrase = task_phrase(body)
        meta.insert(0, e(phrase[0].upper() + phrase[1:]))
    pooled = body.get("reproducibility")
    if pooled:
        meta.append(f"runs made at repo commit {short(pooled.get('repo_sha') or 'none recorded')}")
    meta.append(f'<a href="#{e(anchors.constraint[cid])}">exact rates and intervals</a>')
    blocks = block_table(cid, body)
    return (
        f'<section class="ogroup" data-rule="{e(cid)}" aria-label="{e(cid)}">'
        f'<h3 class="rule">{e(body.get("text", ""))}</h3>'
        f'<p class="ometa">{" &middot; ".join(meta)}</p>'
        f"{outcome_table(cid, body)}{blocks}{BLOCK_NOTE if blocks and explain else ''}</section>"
    )


def split_sentence(results: dict[str, Any], anchors: Anchors) -> str:
    """Task success and the rule are separate: the example is the cell with the most runs that
    broke the rule and still passed the task, from the data. No example when there is none."""
    lead = (
        "Task success and keeping the rule are counted separately, so a run can break the rule "
        "and still count as a task success."
    )
    best = None
    any_outcomes = False
    for cid, body in sorted(results["constraints"].items()):
        if not is_graded(body):
            continue
        for bid, c in ordered_cells(body):
            any_outcomes = any_outcomes or bool(c.get("outcomes"))
            k = passed_anyway(c)
            if k and (best is None or k > best[0]):
                best = (k, cid, bid, c)
    if best is None:
        if not any_outcomes:
            text = (
                f"{lead} This results file has no per-run outcome split, so this page cannot say "
                "how often that happened."
            )
        else:
            text = (
                "Task success and keeping the rule are counted separately. In these results no "
                "run that broke the rule passed the task, so every task success here also kept "
                "the rule."
            )
        return f'<p class="split-note">{text}</p>'
    k, cid, bid, c = best
    body = results["constraints"][cid]
    tasks = task_phrase(body)
    where = f" at <code>{e(bid)}</code>" if len(body["boundaries"]) > 1 else ""
    note = (
        row_note(body, bid, c)
        if len(compaction_settings(body["boundaries"][bid]["cells"])) > 1
        else ""
    )
    under = f", {e(compaction_note(c.get('compaction')))}" if note else ""
    op = c["operator"]
    with_what = "no sabotage" if op == "none" else f"the rule {OPERATOR_PHRASE.get(op, op)}"
    out, n = c["outcomes"], c["n"]
    return (
        f'<p class="split-note">{lead} For example, <a href="#{e(anchors.constraint[cid])}">'
        f"<code>{e(cid)}</code></a>{f' ({e(tasks)})' if tasks else ''}{where}{under}, "
        f"{e(ARM_WORD.get(c['arm'], c['arm']))} runs with {e(with_what)}: "
        f"{e(out['broke'])} of {e(n)} runs broke the rule and {e(k)} of those {e(out['broke'])} "
        f"still passed the task, so task success reads {e(c['successes'])} of {e(n)} while "
        f"{e(out['done'])} of {e(n)} did the task and kept the rule.</p>"
    )


def outcome_legend() -> str:
    return (
        '<ul class="okey" aria-label="Bar key">'
        f'<li><span class="seg done"></span>{e(HEADLINE)}</li>'
        f'<li><span class="seg open"></span>{e(KEPT_OPEN)}</li>'
        '<li><span class="seg broke"></span>Broke the rule, whether or not the task passed</li>'
        "</ul>"
    )


def render_outcomes(results: dict[str, Any], anchors: Anchors, verdict: str) -> str:
    constraints = results["constraints"]
    graded = [cid for cid in sorted(constraints) if is_graded(constraints[cid])]
    ungraded = [cid for cid in sorted(constraints) if not is_graded(constraints[cid])]
    groups, explained = [], False
    for cid in graded:
        groups.append(render_group(cid, constraints[cid], anchors, not explained))
        explained = explained or '<table class="blocks"' in groups[-1]
    legend = outcome_legend()
    skipped = (
        '<p class="note">Not graded, so not shown here (no compiled check, so no run can be '
        "marked as breaking the rule): "
        + ", ".join(f"<code>{e(c)}</code>" for c in ungraded)
        + ".</p>"
        if ungraded
        else ""
    )
    return (
        '<section aria-labelledby="outcomes-h" class="outcomes">'
        '<h2 id="outcomes-h">What happened, rule by rule</h2>'
        f"{split_sentence(results, anchors)}{legend}"
        + "".join(groups)
        + f"{skipped}{verdict}</section><!--/outcomes-->"
    )


EXACT_HOWTO = (
    "Paired cells are forks of the same sources, so the exact test compares matched pairs and "
    "p says how often a split at least this lopsided would turn up if the two arms did the "
    "same thing. Pairs from one source are not independent, so the figure that counts each "
    "source once is the cautious one, and with only a few sources the smallest p it can reach "
    "is limited. Many comparisons are shown on this page and none is corrected for that."
)


def has_exact_lines(results: dict[str, Any]) -> bool:
    """Whether the page will print an exact-test line for these results."""
    return any(
        exact_entries(b)
        for body in results.get("constraints", {}).values()
        if is_graded(body)
        for b in body.get("boundaries", {}).values()
    )


def render_howto(exact: bool = False) -> str:
    items = "".join(f"<li>{t}</li>" for t in HOWTO)
    if exact:
        items += f'<li class="exact-howto">{e(EXACT_HOWTO)}</li>'
    return (
        '<aside class="howto" aria-labelledby="howto-h"><h2 id="howto-h">How to read this</h2>'
        f"<ol>{items}</ol></aside>"
    )


# --- at a glance: the result at the top of a results set ----------------------------------------

DASH = "&mdash;"
GLANCE_COLUMNS = (
    "Broke the rule, no gate",
    "Broke the rule, block-only",
    "Broke the rule, gated",
    "Exact test p (gated vs block-only)",
)
GLANCE_ATTEMPT_COLUMNS = (  # only in a set that has attempts entries
    "Tried the forbidden action, block-only \u2192 gated",
    "Exact test p (tried the forbidden action)",
)
GLANCE_CAPTION = (
    "These are all the graded rules that were tested, not a selection. Each rate is the runs "
    "that broke the rule over the runs made. A rule has one row for each sabotage setting (and "
    "each handoff and compaction setting) it was run under; every row stands alone and no rows "
    "are added together. Where block-only refuses the same action it gets 0 by construction, so "
    "a 0 against 0 is not tested. Read the detail below before relying on a single row."
)


def glance_conditions(b: dict[str, Any]) -> list[tuple[dict[str, Any] | None, str]]:
    """The (compaction setting, operator) pairs of a boundary that have a block-only or gated
    cell: one row each. No sabotage first, then the operators by name."""
    cells = b.get("cells", [])
    out = []
    for setting in compaction_settings(cells):
        ops = {c["operator"] for c in cells if c.get("compaction") == setting}
        blocking = {
            c["operator"]
            for c in cells
            if c.get("compaction") == setting and c["arm"] in BLOCKING_ARMS
        }
        out += [(setting, op) for op in sorted(ops & blocking, key=lambda o: (o != "none", o))]
    return out


def glance_rate(cell: dict[str, Any] | None) -> str:
    """k of n and the share, from the cell the detail tables use; a dash when there is no cell."""
    if not cell or not cell.get("n"):
        return DASH
    return f"{e(cell['violations'])} of {e(cell['n'])} ({pct(cell['violations'] / cell['n'])})"


def glance_attempts(cell: dict[str, Any] | None) -> str:
    """Runs that tried the forbidden action, k of n and the share, from the cell's `arm_metrics`
    (the detail table's figure); a dash when the cell has none."""
    rate = ((cell or {}).get("arm_metrics") or {}).get("attempt_rate")
    if not rate or not rate.get("n"):
        return DASH
    return f"{e(rate['k'])} of {e(rate['n'])} ({pct(rate['k'] / rate['n'])})"


def glance_p(b: dict[str, Any], setting: dict[str, Any] | None, op: str, name: str) -> str:
    """The exact test p for the outcome `name`, gated against block-only, at this operator and
    compaction setting; for a paired test also the figure that counts each source once. A dash
    when there is no such entry, and when both arms have a count of 0: block-only gets 0 by
    construction for an action it refuses, so 0 against 0 is not a test result."""
    for t in exact_entries(b):
        if (
            t["name"] == name
            and t["operator"] == op
            and t.get("compaction") == setting
            and {t["a"]["arm"], t["b"]["arm"]} == set(BLOCKING_ARMS)
        ):
            if t["k_a"] == 0 and t["k_b"] == 0:
                return DASH
            by_source = t.get("p_exact_by_source")
            return e(p_figure(t["p_exact"])) + (
                f'<span class="count">{e(p_figure(by_source))} counting each source once</span>'
                if by_source is not None
                else ""
            )
    return DASH


def glance_rule(cid: str, body: dict[str, Any], withheld: bool) -> str:
    """The rule as the page shows it: its text, or where the text is withheld its id and the
    withheld wording."""
    if withheld:
        return f'<code>{e(cid)}</code> <span class="notrun">{e(WITHHELD)}</span>'
    return e(body.get("text", ""))


def glance_row(
    cid: str,
    body: dict[str, Any],
    bid: str,
    setting: dict[str, Any] | None,
    op: str,
    anchors: Anchors,
    withheld: bool,
    attempts: bool,
) -> str:
    b = body["boundaries"][bid]
    cells = b.get("cells", [])
    note = row_note(body, bid, {"compaction": setting})
    head = (
        f'<th scope="row"><a href="#{e(anchors.section(cid, bid))}">'
        f"{glance_rule(cid, body, withheld)}</a> "
        f'<span class="op">{e(op_row(op))}</span>'
        + (f'<span class="count">{e(note)}</span>' if note else "")
        + "</th>"
    )
    ungated = find_cell(cells, "natural" if op == "none" else "mutated", op, setting)
    block_only = find_cell(cells, "block_only", op, setting)
    gated = find_cell(cells, "gated", op, setting)
    values = (
        glance_rate(ungated),
        glance_rate(block_only),
        glance_rate(gated),
        glance_p(b, setting, op, "violations"),
    )
    if attempts:
        tried = (glance_attempts(block_only), glance_attempts(gated))
        values += (
            DASH if tried == (DASH, DASH) else f"{tried[0]} \u2192 {tried[1]}",
            glance_p(b, setting, op, "attempts"),
        )
    return "<tr>" + head + glance_cells(values, attempts) + "</tr>"


def glance_columns(attempts: bool) -> tuple[str, ...]:
    return GLANCE_COLUMNS + GLANCE_ATTEMPT_COLUMNS if attempts else GLANCE_COLUMNS


def glance_cells(values: tuple[str, ...], attempts: bool) -> str:
    return "".join(
        f'<td data-label="{e(label)}">{value}</td>'
        for label, value in zip(glance_columns(attempts), values, strict=True)
    )


def glance_empty_row(
    cid: str, body: dict[str, Any], anchors: Anchors, withheld: bool, attempts: bool
) -> str:
    """A graded rule with no block-only or gated cell: named, with dashes, never left out."""
    rule = glance_rule(cid, body, withheld)
    bids = sorted(body.get("boundaries", {}))
    if bids:
        rule = f'<a href="#{e(anchors.section(cid, bids[0]))}">{rule}</a>'
    dashes = glance_cells((DASH,) * len(glance_columns(attempts)), attempts)
    return f'<tr><th scope="row">{rule}</th>{dashes}</tr>'


def render_glance(results: dict[str, Any], anchors: Anchors, appendix: int | None = None) -> str:
    """The result of one results set in one table, above everything else of the set: one row per
    graded rule and condition (sabotage setting, handoff, compaction setting), the broke-the-rule
    counts of the ungated, block-only and gated arms and the exact test p. Nothing is added up
    across rows. Empty for a results file without exact tests."""
    if not has_exact_lines(results):
        return ""
    withheld = appendix is not None
    attempts = any(
        t["name"] == "attempts"
        for body in results["constraints"].values()
        if is_graded(body)
        for b in body.get("boundaries", {}).values()
        for t in exact_entries(b)
    )
    rows = []
    for cid in sorted(results["constraints"]):
        body = results["constraints"][cid]
        if not is_graded(body):
            continue
        mine = [
            glance_row(cid, body, bid, setting, op, anchors, withheld, attempts)
            for bid in sorted(body.get("boundaries", {}))
            for setting, op in glance_conditions(body["boundaries"][bid])
        ]
        rows += mine or [glance_empty_row(cid, body, anchors, withheld, attempts)]
    sid = "glance" if appendix is None else f"appendix-{appendix}-glance"
    level = 2 if appendix is None else 3
    widths = (24, 11, 11, 11, 13, 17, 13) if attempts else (31, 15, 15, 15, 24)
    cols = "".join(f'<col style="width:{w}%">' for w in widths)
    table = (
        f'<table class="outcome glance"><colgroup>{cols}</colgroup>'
        '<thead><tr><th scope="col">Rule</th>'
        + "".join(f'<th scope="col">{e(label)}</th>' for label in glance_columns(attempts))
        + "</tr></thead>"
        + f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return (
        f'<section class="ogroup glance" aria-labelledby="{sid}-h">'
        f'<h{level} id="{sid}-h">At a glance</h{level}>'
        + panels.tablewrap("At a glance: every graded rule, broke the rule by arm", table)
        + f'<p class="note">{e(GLANCE_CAPTION)}</p></section>'
    )


def note_paragraphs(notes: str | None) -> list[str]:
    """Plain text split on blank lines, in order, empty paragraphs dropped."""
    return [
        p.strip() for p in re.split(r"\n[ \t]*\n", (notes or "").replace("\r\n", "\n")) if p.strip()
    ]


def render_notes(notes: str | None) -> str:
    """The optional "Notes on these numbers" section. `notes` is plain text, paragraphs separated
    by blank lines; every character is escaped. Nothing (not even a heading) when there is no
    text, so a page without notes is the page it always was."""
    paragraphs = note_paragraphs(notes)
    if not paragraphs:
        return ""
    return (
        '<section aria-labelledby="notes-h" class="notes"><h2 id="notes-h">Notes on these '
        "numbers</h2>" + "".join(f"<p>{e(p)}</p>" for p in paragraphs) + "</section>"
    )


# --- appendices: separate result sets ---------------------------------------------------------

WITHHELD = "Rule text withheld (held-out task)"
APPENDIX_INTRO = (
    "Reported apart from the main set: none of these runs is counted in the results above. "
    "Counts are small; read the intervals."
)
CONDITION_FIELDS = (
    ("repo_sha", "repo commit"),
    ("ledger_hash", "ledger"),
    ("safe_config_hash", "safe config"),
    ("lockfile_hash", "lockfile"),
)


@dataclass(frozen=True)
class Appendix:
    """A second result set shown after the main page. It is read from its own results file only
    and never feeds the main page. `notes` is plain text like `--notes`."""

    title: str
    results: dict[str, Any]
    notes: str | None = None


def value_or_none(value: Any) -> str:
    return short(value) if value else "none recorded"


def appendix_differences(main: dict[str, Any], own: dict[str, Any]) -> str:
    """How this result set's conditions differ from the main set's, read from the two results
    files and nothing else: the reproducibility fields that are not equal, and the mutation
    no-ops (runs whose sabotage changed nothing) that each file counts."""
    here, there = own.get("reproducibility") or {}, main.get("reproducibility") or {}
    items = [
        f"{label} {value_or_none(here.get(key))} here, {value_or_none(there.get(key))} in the "
        "main set"
        for key, label in CONDITION_FIELDS
        if here.get(key) != there.get(key)
    ]
    mine, theirs = here.get("model_ids") or {}, there.get("model_ids") or {}

    def model(mid: Any) -> str:
        return f"<code>{e(mid)}</code>" if mid else "none recorded"

    items += [
        f"{e(role)} model {model(mine.get(role))} here, {model(theirs.get(role))} in the main set"
        for role in sorted(set(mine) | set(theirs))
        if mine.get(role) != theirs.get(role)
    ]
    noops = [r["runs"].get("inconclusive", {}).get("mutation_noop") for r in (own, main)]
    if any(noops):
        a, b = ("none recorded" if n is None else e(n) for n in noops)
        items.append(
            f"mutation no-ops: {a} here, {b} in the main set (a no-op is a run whose sabotage "
            "changed nothing, so it is left out of every rate)"
        )
    body = (
        "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"
        if items
        else "<p>Same conditions as the main set.</p>"
    )
    return (
        "<h3>How these runs differ from the main set</h3>"
        '<p class="note">Read from the two results files only.</p>' + body
    )


def appendix_accounting(results: dict[str, Any]) -> str:
    """Found, counted, left out by reason, and the conditions the runs were pooled under."""
    repro = results.get("reproducibility") or {}
    runs = results["runs"]
    reasons = runs.get("inconclusive", {})
    reason_items = "".join(
        f"<div><dt>{e(REASON_LABEL.get(r, r.replace('_', ' ')))}</dt><dd>{e(reasons[r])}</dd></div>"
        for r in sorted(reasons)
    )
    models = (
        "".join(
            f"<div><dt>{e(role)}</dt><dd><code>{e(mid)}</code></dd></div>"
            for role, mid in sorted((repro.get("model_ids") or {}).items())
        )
        or "<div><dt>models</dt><dd>none recorded</dd></div>"
    )
    hashes = "".join(
        f"<div><dt>{e(label)}</dt><dd>{value_or_none(repro.get(key))}</dd></div>"
        for key, label in CONDITION_FIELDS
    )
    return (
        '<div class="meta"><div><h3>Runs</h3>'
        f'<dl class="kv"><div><dt>found</dt><dd>{e(runs.get("found", 0))}</dd></div>'
        f"<div><dt>counted in the rates</dt><dd>{e(runs.get('pooled', 0))}</dd></div></dl>"
        f'<h3>Left out, by reason</h3><dl class="kv">{reason_items}</dl></div>'
        f'<div><h3>Models</h3><dl class="kv">{models}</dl></div>'
        f'<div><h3>Same conditions</h3><dl class="kv">{hashes}</dl></div></div>'
    )


ACCOUNTING_NOTE = (
    "Each rule's row counts only the runs that were counted: the results file holds no record of "
    "a run that errored or was left out for a single rule, arm or sabotage setting, so those "
    "runs are shown for the whole set only, in the first table and under “Left out, by "
    "reason”. A dash means the file has no figure there."
)


def count_or_dash(value: Any) -> int | None:
    """A whole count from the file, or None for a missing or malformed one (shown as a dash)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def accounting_totals(runs: dict[str, Any]) -> str:
    """Found, counted and the difference, each only if the file supports it."""
    found, counted = count_or_dash(runs.get("found")), count_or_dash(runs.get("pooled"))
    left = found - counted if found is not None and counted is not None else None
    if left is not None and left < 0:
        left = None
    if found is None and counted is None:
        return ""
    body = "".join(
        f'<tr><th scope="row">{e(label)}</th>'
        f'<td data-label="Runs">{DASH if value is None else e(value)}</td></tr>'
        for label, value in (
            ("Run folders found", found),
            ("Finished and counted in the rates", counted),
            ("Found but not counted", left),
        )
    )
    table = (
        '<table class="outcome"><colgroup><col style="width:70%"><col style="width:30%">'
        '</colgroup><thead><tr><th scope="col">Whole set</th><th scope="col">Runs</th></tr>'
        f"</thead><tbody>{body}</tbody></table>"
    )
    return panels.tablewrap("Run accounting: found, counted and not counted", table)


def accounting_by_arm(results: dict[str, Any]) -> str:
    """The counted runs of each rule and sabotage setting by arm, from the cells (a run is in one
    cell, so sums over boundaries and compaction settings count no run twice). An arm with no cell
    is a dash."""
    counts: dict[tuple[str, str, str], int] = {}
    for cid, body in results.get("constraints", {}).items():
        for b in body.get("boundaries", {}).values():
            for cell in b.get("cells", []):
                key = (cid, cell["operator"], cell["arm"])
                counts[key] = counts.get(key, 0) + cell["n"]
    rule_ops = sorted({k[:2] for k in counts}, key=lambda k: (k[0], k[1] != "none", k[1]))
    if not rule_ops:
        return ""
    labels = [ARM_LABEL[arm] for arm in ARM_ORDER] + ["All arms"]
    body = ""
    for cid, op in rule_ops:
        per_arm = [counts.get((cid, op, arm)) for arm in ARM_ORDER]
        total = sum(n for (c, o, _), n in counts.items() if (c, o) == (cid, op))
        values = per_arm + [total]
        body += (
            f'<tr><th scope="row"><code>{e(cid)}</code> {e(op_row(op))}</th>'
            + "".join(
                f'<td data-label="{e(label)}">{DASH if v is None else e(v)}</td>'
                for label, v in zip(labels, values, strict=True)
            )
            + "</tr>"
        )
    cols = "".join(f'<col style="width:{w}%">' for w in (30, 14, 14, 14, 14, 14))
    table = (
        f'<table class="outcome"><colgroup>{cols}</colgroup><thead><tr>'
        '<th scope="col">Rule and sabotage setting</th>'
        + "".join(f'<th scope="col">{e(label)}</th>' for label in labels)
        + f"</tr></thead><tbody>{body}</tbody></table>"
    )
    return panels.tablewrap("Run accounting: counted runs by rule, sabotage setting and arm", table)


def run_accounting(results: dict[str, Any], index: int) -> str:
    """The set's run accounting as tables, every figure computed from its own results file: what
    was found, counted and not counted, and the counted runs by rule, sabotage setting and arm.
    Nothing a file does not carry is shown (a dash), and a file with none of it gets no section."""
    inner = accounting_totals(results.get("runs") or {}) + accounting_by_arm(results)
    if not inner:
        return ""
    sid = f"appendix-{index}-accounting"
    return (
        f'<section class="ogroup accounting" aria-labelledby="{sid}-h">'
        f'<h3 id="{sid}-h">Run accounting</h3>{inner}'
        f'<p class="note">{e(ACCOUNTING_NOTE)}</p></section>'
    )


def appendix_rule(cid: str, body: dict[str, Any], anchors: Anchors, explain: bool) -> str:
    """One rule of an appendix: its id, kind and counts. The rule's wording is never printed."""
    graded = is_graded(body)
    boundaries = body.get("boundaries", {})
    meta = [f"rule <code>{e(cid)}</code>"]
    pooled = body.get("reproducibility")
    if pooled:
        meta.append(f"runs made at repo commit {value_or_none(pooled.get('repo_sha'))}")
    if graded:
        blocks = block_table(cid, body)
        inner = (
            outcome_table(cid, body)
            + blocks
            + (BLOCK_NOTE if blocks and explain else "")
            + "".join(
                graded_section(cid, bid, boundaries[bid], anchors, None, detail=False)
                for bid in sorted(boundaries)
            )
        )
    else:
        inner = "".join(
            ungraded_section(cid, bid, boundaries[bid], anchors) for bid in sorted(boundaries)
        )
    return (
        f'<section class="ogroup" data-rule="{e(cid)}" aria-label="{e(cid)}">'
        f'<h3 class="rule"><code>{e(cid)}</code> '
        f'<span class="badge">{e(body.get("kind", "unknown kind"))}</span></h3>'
        f'<p class="note withheld">{e(WITHHELD)}</p>'
        f'<p class="ometa">{" &middot; ".join(meta)}</p>{inner}</section>'
    )


def render_appendix(index: int, appendix: Appendix, main: dict[str, Any]) -> str:
    """One separately labelled result set. Ids are prefixed by the appendix number, so two sets
    with the same rule ids do not collide. Nothing here reads run folders or trace data."""
    results = appendix.results
    try:
        check_format(results)
    except ValueError as exc:
        raise ValueError(f"appendix {appendix.title!r}: {exc}") from exc
    sid = f"appendix-{index}"
    constraints = results["constraints"]
    anchors = Anchors(constraints, prefix=f"a{index}-")
    rules, explained = [], False
    for cid in sorted(constraints):
        rules.append(appendix_rule(cid, constraints[cid], anchors, not explained))
        explained = explained or '<table class="blocks"' in rules[-1]
    paragraphs = "".join(f"<p>{e(p)}</p>" for p in note_paragraphs(appendix.notes))
    return (
        f'<section class="appendix" id="{sid}" aria-labelledby="{sid}-h">'
        f'<h2 id="{sid}-h">{e(appendix.title)}</h2>'
        f'<p class="note">{e(APPENDIX_INTRO)}</p>{paragraphs}'
        f"{render_glance(results, anchors, index)}{appendix_accounting(results)}"
        f"{run_accounting(results, index)}{appendix_differences(main, results)}"
        f"<h3>What happened, rule by rule</h3>{outcome_legend()}" + "".join(rules) + "</section>"
    )


# --- page -------------------------------------------------------------------------------------

CSS = (Path(__file__).parent / "site.css").read_text(encoding="utf-8")
# Only for a page that has a presence grid, so a page without one is the page it always was.
PRESENCE_CSS = (Path(__file__).parent / "presence.css").read_text(encoding="utf-8")
# Only for a page that has an appendix, for the same reason.
APPENDIX_CSS = (Path(__file__).parent / "appendix.css").read_text(encoding="utf-8")


def render(
    results: dict[str, Any],
    *,
    trace: panels.TraceEvidence | None = None,
    repairs: list[panels.RepairView] | None = None,
    ledger: Ledger | None = None,
    notes: str | None = None,
    appendices: Sequence[Appendix] = (),
    recalls: Sequence[panels.RecallView] = (),
) -> str:
    """The page. `trace` (from --runs), `repairs` (--repair) and `ledger` (--ledger) are optional
    evidence; without `trace` the constraint cards have no trace panel, without `repairs` the
    patch panel says there is no repair run yet. `recalls` (--recall) show the extraction recall
    measured offline, only when the results file carries none. `notes` (--notes) is free text
    shown after the results, escaped. `appendices` (--appendix) are separate result sets appended
    after everything else; nothing in them reaches the rest of the page."""
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
    outcomes = (
        render_outcomes(results, anchors, render_verdict(results, anchors))
        if constraints
        else render_verdict(results, anchors)
    )
    found = trace.found_by_rule if trace else None
    exact = has_exact_lines(results) or any(has_exact_lines(a.results) for a in appendices)
    body = (
        f"{render_top(exact, render_glance(results, anchors))}{outcomes}{render_notes(notes)}"
        f"{render_key()}{render_fixes()}"
        f"{render_header(results, recalls)}"
        f"{panels.render_coverage(results, ledger, found, list(recalls))}"
        f"{graph_section}"
        f"{panels.render_patch_panel(repairs)}{render_method(results)}"
    )
    if appendices:
        body += "<!--appendices-->" + "".join(
            render_appendix(i, a, results) for i, a in enumerate(appendices, start=1)
        )
    css = CSS + PRESENCE_CSS if 'class="presence"' in body else CSS
    if appendices:
        css += APPENDIX_CSS
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        "<title>NemoGate: rule survival across handoffs</title>"
        f"<style>{css}</style></head><body><main>{body}</main></body></html>\n"
    )


def numbers_in(text: str) -> list[str]:
    """The number tokens of `text`, in order: a maximal run of digits (of any script, as
    `str.isdigit` sees them) with an optional decimal part, a "." followed by more digits. A full
    stop after a number is not a decimal point. Tokens are compared as typed, so 66 is not 660 and
    the Arabic-Indic five is not 5.

    The one allowance: a digit run directly after a letter, or after a letter and a hyphen, is
    part of an identifier (a label, never a statistic) and is left out, decimal part included.
    It knows nothing of what any identifier means. The same rule applies to the page's text, so
    the digits of a hash fragment on the page do not vouch for a number either."""
    found, i, n = [], 0, len(text)
    while i < n:
        if not text[i].isdigit():
            i += 1
            continue
        j = i
        while j < n and text[j].isdigit():
            j += 1
        if j + 1 < n and text[j] == "." and text[j + 1].isdigit():
            j += 1
            while j < n and text[j].isdigit():
                j += 1
        before = text[max(i - 2, 0) : i]
        if not (before[-1:].isalpha() or (before[-1:] == "-" and before[:-1].isalpha())):
            found.append(text[i:j])
        i = j
    return found


def visible_numbers(markup: str) -> set[str]:
    """The numbers in the text a reader sees: tags and style dropped, entities decoded."""
    markup = re.sub(r"<style>.*?</style>", " ", markup, flags=re.S)
    return set(numbers_in(html.unescape(re.sub(r"<[^>]+>", " ", markup))))


def notes_problems(
    results: dict[str, Any],
    appendices: Sequence[Appendix],
    files: Sequence[tuple[Any, str | None]],
    **evidence: Any,
) -> list[str]:
    """One message per notes file that holds a number the page does not show. Every number on the
    page comes from the results file, so a note may only repeat one. `files` is (path, text) for
    the main notes, then one per appendix, in order (path None: no notes). The numbers a note is
    held to are those of its own set, read off the page as built with no notes at all: the main
    part for `--notes`, the appendix's own section for its notes, so a note cannot vouch for
    itself or borrow a number from another set. A message names the file and, per line, the
    unmatched numbers; never the line, because appendix notes can sit next to withheld rule text."""
    if not any(path and text for path, text in files):
        return []
    page = render(
        results,
        notes=None,
        appendices=[Appendix(a.title, a.results) for a in appendices],
        **evidence,
    )
    body = page[page.index("<body>") :]
    parts = re.split(r'(?=<section class="appendix" id="appendix-)', body)
    problems = []
    for (path, text), part in zip(files, parts, strict=True):
        if not path or not text:
            continue
        shown = visible_numbers(part)
        bad = []
        for number, line in enumerate(text.split("\n"), 1):
            missing = [n for n in dict.fromkeys(numbers_in(line)) if n not in shown]
            if missing:
                bad.append(f"line {number}: {', '.join(missing)}")
        if bad:
            problems.append(
                f"{path}: numbers the results do not hold ({'; '.join(bad)}); "
                "a note may only repeat a number the page shows"
            )
    return problems


def load_appendix(title: str, results_file: str, notes_file: str | None = None) -> Appendix:
    """Read an appendix's results (and notes) file. OSError or ValueError on anything unusable."""
    results = json.loads(Path(results_file).read_text(encoding="utf-8"))
    if not isinstance(results, dict):
        raise ValueError(f"appendix {title!r}: {results_file} is not a results object")
    notes = Path(notes_file).read_text(encoding="utf-8") if notes_file else None
    return Appendix(title, results, notes)


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
        "--recall",
        type=Path,
        nargs="+",
        metavar="FILE",
        help="extraction recall files (python -m ledger.recall output), one per set: shown when "
        "results.json has no recall, with the note that it is not a held-out measure",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        help="the ledger the runs used: lists unchecked and unexercised rules",
    )
    parser.add_argument(
        "--notes",
        type=Path,
        metavar="FILE",
        help="plain text, paragraphs split by blank lines: shown as 'Notes on these numbers'",
    )
    parser.add_argument(
        "--appendix",
        action="append",
        nargs="+",
        default=[],
        metavar=("TITLE", "RESULTS_FILE"),
        help="TITLE RESULTS_FILE [NOTES_FILE], repeatable: a separate result set shown after the "
        "page, never pooled into it. Rule wording is withheld; no run folders are read",
    )
    args = parser.parse_args(argv)
    for values in args.appendix:
        if not 2 <= len(values) <= 3:
            parser.error("--appendix takes TITLE RESULTS_FILE [NOTES_FILE]")
    try:
        notes = args.notes.read_text(encoding="utf-8") if args.notes else None
        appendices = [load_appendix(*values) for values in args.appendix]
        results = json.loads(args.results.read_text(encoding="utf-8"))
        evidence = {
            "trace": panels.load_trace_evidence(
                args.runs, results.get("reproducibility"), rule_groups(results)
            )
            if args.runs
            else None,
            "repairs": panels.load_repairs(args.repair) if args.repair else None,
            "ledger": load_ledger_yaml(args.ledger.read_text(encoding="utf-8"))
            if args.ledger
            else None,
            "recalls": panels.load_recall(args.recall) if args.recall else (),
        }
        problems = notes_problems(
            results,
            appendices,
            [(args.notes, notes)]
            + [
                (v[2] if len(v) == 3 else None, a.notes)
                for v, a in zip(args.appendix, appendices, strict=True)
            ],
            **evidence,
        )
        if problems:
            for problem in problems:
                print(f"report.site: {problem}", file=sys.stderr)
            return 2
        page = render(results, notes=notes, appendices=appendices, **evidence)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(page, encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"report.site: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
