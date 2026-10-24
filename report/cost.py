"""The price of one prevented rule break: "Cost of prevention" on the results page.

Pure functions over the cells of a results file (`replay/README.md`, "stats.py"); no file, no
clock, no model. For one rule at one sabotage setting a row compares an arm that blocks (gated,
block-only) with the arm at the same setting that has no gate (mutated at a sabotage operator,
natural at "none"):

- *prevented* = violation rate of the ungated arm minus that of the compared arm, as a fraction
  of runs, computed from the counts;
- *extra cost per run* = mean cost per run (`arm_metrics.cost`) of the compared arm minus the
  ungated arm's;
- *price* = extra cost / prevented, only when both are above 0. Otherwise there is no price and
  `reason` says why in words: never a division by zero, never a negative price.

Like with like. A fork's cost covers only what ran after the fork point, a source run's all of
it, so an ungated and a compared cell are priced together only when both are whole runs or both
are all forks (`n_forks` in the cell; a cell with some forks is neither). A pair that is not
alike, or whose cells lack the fields, gets no row; a pair that has cost figures but is not alike
is counted (`price_condition` returns the count) so the page can say it left them out.

The "widest reading" of a price takes the ends of the two 95% intervals the file already holds
(Wilson for the rates, bootstrap for the costs) the worst and the best way: the cheapest end
needs the least extra cost over the most prevented, the dearest the most extra cost over the
fewest prevented. It is wider than an interval made for the price and says so. A cost on the
`range` basis (fewer than five runs) is a spread, not an interval, so no widest reading is
drawn from it. An end of the price that the intervals cannot pin down is None: `price_lo` None
means the gate may have cost nothing or less, `price_hi` None means the prevented rate may have
been 0.
"""

import html
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

ARMS = ("gated", "block_only")  # the arms that block, in the order the table shows them
ARM_NAME = {"gated": "Gated", "block_only": "Block-only"}
MIN_RUNS = 5  # below this the cost is a range, not a 95% interval (replay/intervals.py)
DASH = "&mdash;"


@dataclass(frozen=True)
class Rate:
    k: int
    n: int
    lo: float
    hi: float


@dataclass(frozen=True)
class PriceRow:
    arm: str
    cover: str  # "full" (whole runs) or "fork" (only what ran after the fork point)
    without: Rate  # the ungated arm
    with_arm: Rate  # the compared arm
    prevented: float  # fraction of runs; negative when the compared arm broke the rule more
    prevented_lo: float
    prevented_hi: float
    extra: float  # dollars per run; negative when the compared arm cost less
    extra_lo: float | None  # None unless both costs are 95% bootstrap intervals
    extra_hi: float | None
    price: float | None
    price_lo: float | None  # None: the gate may have cost nothing or less
    price_hi: float | None  # None: the prevented rate may have been 0
    has_bounds: bool  # whether price_lo and price_hi mean anything (even when None)
    reason: str  # why there is no price; "" when there is one
    small: bool  # fewer than MIN_RUNS runs in either cell
    cost_is_range: bool  # a cost is a lowest-to-highest range or a single value, not an interval


def _cost(cell: dict[str, Any]) -> dict[str, Any]:
    return (cell.get("arm_metrics") or {}).get("cost") or {}


def _usable(cell: dict[str, Any] | None) -> bool:
    """A cell with runs, a violation count with its interval and a mean cost."""
    if not cell or not cell.get("n"):
        return False
    cvr = cell.get("cvr") or {}
    return (
        cell.get("violations") is not None
        and cvr.get("lo") is not None
        and cvr.get("hi") is not None
        and _cost(cell).get("mean") is not None
    )


def _cover(cell: dict[str, Any]) -> str:
    forks = cell.get("n_forks") or 0
    if forks == 0:
        return "full"
    return "fork" if forks == cell["n"] else "mixed"


def _find(cells: Sequence[dict[str, Any]], arm: str, op: str, setting: Any) -> dict | None:
    for c in cells:
        if c.get("arm") == arm and c.get("operator") == op and c.get("compaction") == setting:
            return c
    return None


def _rate(cell: dict[str, Any]) -> Rate:
    return Rate(cell["violations"], cell["n"], cell["cvr"]["lo"], cell["cvr"]["hi"])


def _row(ungated: dict[str, Any], cell: dict[str, Any], arm: str, cover: str) -> PriceRow:
    without, with_arm = _rate(ungated), _rate(cell)
    prevented = without.k / without.n - with_arm.k / with_arm.n
    prevented_lo, prevented_hi = without.lo - with_arm.hi, without.hi - with_arm.lo
    u_cost, c_cost = _cost(ungated), _cost(cell)
    extra = c_cost["mean"] - u_cost["mean"]
    intervals = all(
        s.get("basis") == "bootstrap" and s.get("lo") is not None and s.get("hi") is not None
        for s in (u_cost, c_cost)
    )
    extra_lo = c_cost["lo"] - u_cost["hi"] if intervals else None
    extra_hi = c_cost["hi"] - u_cost["lo"] if intervals else None
    price: float | None = None
    reason = ""
    if prevented == 0:
        reason = "no violations prevented here"
    elif prevented < 0:
        reason = "more violations with the gate than without"
    elif extra < 0:
        reason = f"{ARM_NAME[arm].lower()} cost less per run"
    elif extra == 0:
        reason = "no extra cost per run"
    else:
        price = extra / prevented
    price_lo = price_hi = None
    if price is not None and intervals:
        if extra_lo > 0 and prevented_hi > 0:
            price_lo = extra_lo / prevented_hi
        if prevented_lo > 0:
            price_hi = max(extra_hi, 0.0) / prevented_lo
    return PriceRow(
        arm=arm,
        cover=cover,
        without=without,
        with_arm=with_arm,
        prevented=prevented,
        prevented_lo=prevented_lo,
        prevented_hi=prevented_hi,
        extra=extra,
        extra_lo=extra_lo,
        extra_hi=extra_hi,
        price=price,
        price_lo=price_lo,
        price_hi=price_hi,
        has_bounds=price is not None and intervals,
        reason=reason,
        small=min(without.n, with_arm.n) < MIN_RUNS,
        cost_is_range=not intervals,
    )


def price_condition(
    cells: Sequence[dict[str, Any]], setting: Any, op: str
) -> tuple[list[PriceRow], int]:
    """The rows (gated, then block-only) of one rule at one operator and compaction `setting`,
    and how many compared cells were left out for not being made like the ungated one."""
    ungated = _find(cells, "natural" if op == "none" else "mutated", op, setting)
    if not _usable(ungated):
        return [], 0
    rows, skipped = [], 0
    for arm in ARMS:
        cell = _find(cells, arm, op, setting)
        if not _usable(cell):
            continue
        cover = _cover(ungated)
        if cover == "mixed" or cover != _cover(cell):
            skipped += 1
        else:
            rows.append(_row(ungated, cell, arm, cover))
    return rows, skipped


# --- the table --------------------------------------------------------------------------------

COLUMNS = (
    "Arm",
    "Broke the rule, no gate → this arm",
    "Prevented per 100 runs",
    "Extra cost per run",
    "Cost per prevented violation",
    "What the cost covers",
)
WIDTHS = (20, 9, 19, 12, 16, 14, 10)  # the rule's column first
CAPTION = (
    "What it cost, in these runs, to stop one rule being broken: a price on this reference app "
    "and these rules, not a price list. Each row sets an arm that blocks against the arm at "
    "the same sabotage setting that has no gate, in runs made the same way, and counts the "
    "cost each run recorded (the last column says whether that is whole runs or only what ran "
    "after the handoff). The rates and the prices come from the same runs. A widest reading "
    "joins the ends of the two 95% intervals the worst and the best way, so it is wider than "
    "an interval made for the price. Read the detail below before relying on one row."
)
SKIPPED = (
    "{count} comparison{s} left out: the arms were not made the same way (whole runs against "
    "forks, which cover only what ran after the handoff, or a mix), so their costs cannot be "
    "set side by side. Their rates are in the table above."
)


def e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _half_up(x: float) -> int:
    """x as a whole number, halves rounded up (0.125 -> 13 when x is a fraction times 100)."""
    return math.floor(x + 0.5 + 1e-9)


def _share(k: int, n: int) -> str:
    return f"{k} of {n} ({_half_up(100 * k / n)}%)"


def _pct(x: float) -> str:
    return f"{_half_up(100 * x)}%"


def _per_run(x: float) -> str:
    return f"${abs(x):.4f}"


def _price(x: float) -> str:
    return "less than $0.001" if x < 0.0005 else f"${x:.3f}"


def _note(text: str) -> str:
    return f'<span class="count">{e(text)}</span>'


def _side(x: float) -> str:
    """One end of an extra cost, in words, so a negative end is never a negative price."""
    if x < 0:
        return f"{_per_run(x)} less"
    return f"{_per_run(x)} more" if x > 0 else "no difference"


def _rates_cell(row: PriceRow) -> str:
    a, b = row.without, row.with_arm
    return f"{e(_share(a.k, a.n))} → {e(_share(b.k, b.n))}" + _note(
        f"95% intervals {_pct(a.lo)} to {_pct(a.hi)}, {_pct(b.lo)} to {_pct(b.hi)}"
    )


def _prevented_cell(row: PriceRow) -> str:
    per100 = _half_up(abs(row.prevented) * 100)
    if row.prevented < 0:
        return e(f"{per100} more broke it with this arm") + _note("so nothing was prevented")
    out = e(str(per100))
    lo, hi = max(row.prevented_lo, 0.0), min(row.prevented_hi, 1.0)
    low = "no gain" if lo <= 0 else str(_half_up(lo * 100))
    return out + _note(f"widest reading: {low} to {_half_up(hi * 100)}")


def _extra_cell(row: PriceRow) -> str:
    arm = ARM_NAME[row.arm]
    if row.extra < 0:
        out = e(f"{arm} cost {_per_run(row.extra)} less per run")
    elif row.extra == 0:
        out = e("no extra cost per run")
    else:
        out = e(f"{_per_run(row.extra)} more per run")
    if row.extra_lo is None or row.extra_hi is None:
        return out
    return out + _note(f"widest reading: {_side(row.extra_lo)} to {_side(row.extra_hi)}")


def _price_cell(row: PriceRow) -> str:
    if row.price is None:
        return f"{DASH}{_note(row.reason)}"
    out = e(f"about {_price(row.price)}")
    if not row.has_bounds:
        return out + _note("no interval for the price")
    low = "may cost less" if row.price_lo is None else _price(row.price_lo)
    high = "no upper limit" if row.price_hi is None else _price(row.price_hi)
    return out + _note(f"widest reading: {low} to {high}")


def _basis_cell(row: PriceRow) -> str:
    out = e("Whole runs" if row.cover == "full" else "After the fork point only")
    if row.small:
        out += _note(f"fewer than {MIN_RUNS} runs")
    if row.cost_is_range:
        out += _note("cost is a range, not a 95% interval")
    return out


def _cells(row: PriceRow) -> str:
    values = (
        e(ARM_NAME[row.arm]),
        _rates_cell(row),
        _prevented_cell(row),
        _extra_cell(row),
        _price_cell(row),
        _basis_cell(row),
    )
    return "".join(
        f'<td data-label="{e(label)}">{value}</td>'
        for label, value in zip(COLUMNS, values, strict=True)
    )


def render_section(
    items: Sequence[tuple[Any, PriceRow]],
    skipped: int,
    head: Callable[[Any, PriceRow], str],
    sid: str,
    level: int,
    wrap: Callable[[str, str], str],
) -> str:
    """The section: a table of one row per (rule condition, arm), the caption, and a line for the
    comparisons left out. `items` pairs an opaque location with its row; `head(location, row)`
    returns the escaped HTML inside the rule's `<th scope="row">`. `wrap(label, table)` boxes
    the table (panels.tablewrap). Empty when there is nothing to say."""
    if not items and not skipped:
        return ""
    table = ""
    if items:
        cols = "".join(f'<col style="width:{w}%">' for w in WIDTHS)
        rows = "".join(
            f'<tr><th scope="row">{head(where, row)}</th>{_cells(row)}</tr>' for where, row in items
        )
        table = wrap(
            "Cost of prevention: price per prevented violation, by rule and arm",
            f'<table class="outcome cost"><colgroup>{cols}</colgroup><thead><tr>'
            '<th scope="col">Rule</th>'
            + "".join(f'<th scope="col">{e(label)}</th>' for label in COLUMNS)
            + f"</tr></thead><tbody>{rows}</tbody></table>",
        )
    left_out = (
        f'<p class="note">{e(SKIPPED.format(count=skipped, s="" if skipped == 1 else "s"))}</p>'
        if skipped
        else ""
    )
    return (
        f'<section class="ogroup cost" aria-labelledby="{e(sid)}-h">'
        f'<h{level} id="{e(sid)}-h">Cost of prevention</h{level}>'
        f'{table}<p class="note">{e(CAPTION)}</p>{left_out}</section>'
    )
