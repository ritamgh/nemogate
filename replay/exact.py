"""Exact significance tests for two arms: Fisher for independent runs, McNemar for forks.

`fisher_exact` and `mcnemar_exact` are the two p-values (stdlib only, `math.comb`).
`compare_arms` picks the test for a pair of arms from where their runs came from and returns the
dict the stats card writes under `exact_tests` (replay/README.md, "Exact tests"). Pure: no file,
no clock, no model.

Which test: if both arms hold forks of the same natural source, the arms saw the same prefix and
a fork of one arm has a partner in the other, so the unit is the matched pair and the test is
McNemar on the pairs that disagree. Otherwise the arms are independent samples and the test is
Fisher on the counts. Pairs from one source are not independent of each other (the same source
under two operators), so a paired result also carries a sign test over sources.
"""

import math
from collections import Counter
from collections.abc import Sequence
from typing import Any, NamedTuple

def fisher_exact(a: int, n1: int, b: int, n2: int) -> float:
    """Two-sided exact p for `a` of `n1` against `b` of `n2` (Fisher's exact test).

    The sum of the probabilities of every 2x2 table with these margins whose probability is at
    most the observed table's, capped at 1. ValueError for an empty arm or a
    count outside its sample.
    """
    if n1 < 1 or n2 < 1:
        raise ValueError(f"each arm needs at least one run, got n1={n1}, n2={n2}")
    if not (0 <= a <= n1 and 0 <= b <= n2):
        raise ValueError(f"counts must be within their samples, got {a}/{n1} and {b}/{n2}")
    events = a + b
    # weight(x) is the number of ways to put x of the events in the first arm; the
    # probability is weight / C(n1 + n2, events), so the weights are compared directly.
    weight = {
        x: math.comb(n1, x) * math.comb(n2, events - x)
        for x in range(max(0, events - n2), min(n1, events) + 1)
    }
    # the weights are exact integers, so "as probable as observed" is an exact comparison
    # (a float tolerance would overflow on large tables)
    extreme = sum(w for w in weight.values() if w <= weight[a])
    return min(1.0, extreme / math.comb(n1 + n2, events))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p for `b` and `c` discordant pairs: binomial(b + c, 1/2).

    1.0 when nothing is discordant; capped at 1. ValueError for a negative count.
    """
    if b < 0 or c < 0:
        raise ValueError(f"discordant counts cannot be negative, got {b} and {c}")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, 2 * tail / 2**n)


class Obs(NamedTuple):
    """One run's outcome as `compare_arms` reads it."""

    source: str | None  # the natural run it was forked from; None if it is not a fork
    operator: str  # the operator of its cell
    event: bool | None  # did the outcome happen; None = could not be read (not "no")


def _matched(a: Sequence[Obs], b: Sequence[Obs]) -> list[tuple[str, bool, bool]]:
    """(source, event in a, event in b) for every fork of a (source, operator) that has exactly
    one fork in each arm and a known outcome on both sides, sorted. A key with two forks in
    one arm is ambiguous (the run folder records no seed to tell them apart) and is left out."""

    def by_key(arm: Sequence[Obs]) -> dict[tuple[str, str], list[Obs]]:
        out: dict[tuple[str, str], list[Obs]] = {}
        for o in arm:
            if o.source is not None:
                out.setdefault((o.source, o.operator), []).append(o)
        return out

    in_a, in_b = by_key(a), by_key(b)
    pairs = []
    for key in sorted(in_a.keys() & in_b.keys()):
        if len(in_a[key]) == 1 == len(in_b[key]):
            ea, eb = in_a[key][0].event, in_b[key][0].event
            if ea is not None and eb is not None:
                pairs.append((key[0], ea, eb))
    return pairs


def compare_arms(a: Sequence[Obs], b: Sequence[Obs]) -> dict[str, Any] | None:
    """The exact test of arm `a` against arm `b` on one outcome, or None if there is nothing to
    test (no known outcome on a side, or no matched pair).

    Keys: `test` ("fisher" or "mcnemar"), `k_a`/`n_a`/`k_b`/`n_b` (events over runs read; for
    McNemar over the matched pairs), `p_exact`, and for McNemar only (None for Fisher):
    `n_pairs`, `n_unmatched` (runs of either arm not in a pair), `discordant` ({"a_only",
    "b_only"}: pairs where only that arm had the event), `n_sources` (sources with a pair),
    `by_source` ({"a_only", "b_only", "ties"}: each source collapsed to the arm that had the
    event in more of its pairs) and `p_exact_by_source` (`mcnemar_exact` on those, ties left out).
    """
    shared = {o.source for o in a if o.source} & {o.source for o in b if o.source}
    if not shared:
        events_a = [o.event for o in a if o.event is not None]
        events_b = [o.event for o in b if o.event is not None]
        if not events_a or not events_b:
            return None
        k_a, k_b = sum(events_a), sum(events_b)
        return {
            "test": "fisher",
            "k_a": k_a,
            "n_a": len(events_a),
            "k_b": k_b,
            "n_b": len(events_b),
            "p_exact": fisher_exact(k_a, len(events_a), k_b, len(events_b)),
            "n_pairs": None,
            "n_unmatched": None,
            "discordant": None,
            "n_sources": None,
            "by_source": None,
            "p_exact_by_source": None,
        }
    pairs = _matched(a, b)
    if not pairs:
        return None
    a_only = sum(ea and not eb for _, ea, eb in pairs)
    b_only = sum(eb and not ea for _, ea, eb in pairs)
    net: Counter[str] = Counter()
    for source, ea, eb in pairs:
        net[source] += int(ea) - int(eb)
    sources = {
        "a_only": sum(v > 0 for v in net.values()),
        "b_only": sum(v < 0 for v in net.values()),
        "ties": sum(v == 0 for v in net.values()),
    }
    return {
        "test": "mcnemar",
        "k_a": sum(ea for _, ea, _ in pairs),
        "n_a": len(pairs),
        "k_b": sum(eb for _, _, eb in pairs),
        "n_b": len(pairs),
        "p_exact": mcnemar_exact(a_only, b_only),
        "n_pairs": len(pairs),
        "n_unmatched": len(a) + len(b) - 2 * len(pairs),
        "discordant": {"a_only": a_only, "b_only": b_only},
        "n_sources": len(net),
        "by_source": sources,
        "p_exact_by_source": mcnemar_exact(sources["a_only"], sources["b_only"]),
    }
