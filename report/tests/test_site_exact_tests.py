"""report/site.py: the exact-test lines. `replay.stats` writes an optional `exact_tests` list per
boundary; the page prints one plain line per entry beside the gated-minus-block-only interval
lines, taking every number and arm name from the entry. Fixture entries are hand-built; every
expected number below was checked by hand (McNemar: 2 * P(X <= 1 | n = 6, 1/2) = 14/64)."""

import copy
import re

import pytest

from report import site
from report.tests.test_site import load, text_of
from report.tests.test_site_outcome_first import blocks_page, group

BID = "delegation:planner->coder"


def arms(a: str = "gated", b: str = "block_only", op: str = "drop") -> dict:
    return {"a": {"arm": a, "operator": op}, "b": {"arm": b, "operator": op}}


def fisher(name, op, k_a, n_a, k_b, n_b, p, a="gated", b="block_only", compaction=None) -> dict:
    return {
        "name": name,
        "operator": op,
        "compaction": compaction,
        **arms(a, b, op),
        "test": "fisher",
        "k_a": k_a,
        "n_a": n_a,
        "k_b": k_b,
        "n_b": n_b,
        "n_pairs": None,
        "n_unmatched": None,
        "discordant": None,
        "n_sources": None,
        "by_source": None,
        "p_exact": p,
        "p_exact_by_source": None,
    }


def mcnemar(
    name, op, a_only, b_only, p, n_sources, ties, by_a, by_b, p_src, a="gated", b="block_only"
) -> dict:
    return {
        "name": name,
        "operator": op,
        "compaction": None,
        **arms(a, b, op),
        "test": "mcnemar",
        "k_a": a_only + 3,
        "n_a": a_only + b_only + 3,
        "k_b": b_only + 3,
        "n_b": a_only + b_only + 3,
        "n_pairs": a_only + b_only + 2,
        "n_unmatched": 0,
        "discordant": {"a_only": a_only, "b_only": b_only},
        "n_sources": n_sources,
        "by_source": {"a_only": by_a, "b_only": by_b, "ties": ties},
        "p_exact": p,
        "p_exact_by_source": p_src,
    }


FISHER_V = fisher("violations", "drop", 0, 4, 1, 2, 0.333333)
# 6 discordant pairs (1 gated-only, 5 block-only-only), 4 sources: 1 to gated, 3 to block-only
MCNEMAR_V = mcnemar("violations", "drop", 1, 5, 0.21875, 4, 0, 1, 3, 0.625)


def page_data(*entries: dict) -> dict:
    data = blocks_page()
    data["constraints"]["C-900"]["boundaries"][BID]["exact_tests"] = list(entries)
    return data


def lines(page: str, cid: str = "C-900") -> list[str]:
    """The text of every exact-test paragraph of one rule's group, in page order."""
    block = group(page, cid)
    paras = re.findall(r'<p class="note exact">(.*?)</p>', block, flags=re.S)
    return [text_of(p).strip() for p in paras]


# --- the lines ----------------------------------------------------------------------------------


def test_fisher_line_has_the_outcome_both_counts_and_p_from_the_entry():
    # Breaks if the independent-runs line is hand-typed, drops a count, or swaps the arms.
    out = lines(site.render(page_data(FISHER_V)))

    assert out == [
        "Exact test (Fisher, independent runs; drop): broke the rule in 0 of 4 gated runs "
        "against 1 of 2 block-only runs, p = 0.333."
    ]


def test_mcnemar_line_has_the_pairs_both_p_values_and_the_source_floor():
    # Breaks if the paired line shows only one p, mixes up whose count is whose, or leaves out
    # the smallest p 4 sources can reach (2 * 0.5^4 = 0.125).
    out = lines(site.render(page_data(MCNEMAR_V)))

    assert out == [
        "Exact test (McNemar, matched forks of the same sources; drop): in 8 matched pairs, "
        "only block-only broke the rule in 5, only gated in 1, p = 0.219 over the pairs; "
        "counting each of 4 sources once, p = 0.625 (with 4 sources the smallest p possible "
        "is 0.125)."
    ]


def test_swapping_a_and_b_swaps_who_is_who_in_both_kinds_of_line():
    # Breaks if the code assumes `a` is gated: the entry names its own arms.
    swapped_f = fisher("violations", "drop", 1, 2, 0, 4, 0.333333, a="block_only", b="gated")
    swapped_m = mcnemar(
        "violations", "drop", 5, 1, 0.21875, 4, 0, 3, 1, 0.625, a="block_only", b="gated"
    )
    out = lines(site.render(page_data(swapped_f, swapped_m)))

    assert "broke the rule in 1 of 2 block-only runs against 0 of 4 gated runs" in out[0]
    assert "only gated broke the rule in 1, only block-only in 5" in out[1]


def test_outcome_words_for_attempts_and_done():
    # Breaks if an outcome is described with another outcome's words.
    page = site.render(
        page_data(
            fisher("attempts", "drop", 0, 4, 1, 2, 0.333333),
            fisher("done", "drop", 4, 4, 1, 2, 0.060606),
        )
    )
    out = lines(page)

    assert "tried the forbidden action in 0 of 4 gated runs against 1 of 2 block-only" in out[0]
    assert "did the job and kept the rule in 4 of 4 gated runs against 1 of 2 block-only" in out[1]
    assert "p = 0.0606" in out[1]


@pytest.mark.parametrize(
    ("p", "text"),
    [
        (1.0, "p = 1"),
        (0.5, "p = 0.5"),
        (0.0625, "p = 0.0625"),
        (0.333333, "p = 0.333"),
        (0.21875, "p = 0.219"),
        (0.0123456, "p = 0.0123"),
        (0.001, "p = 0.001"),
        (0.000999, "p < 0.001"),
        (0.0, "p < 0.001"),
    ],
)
def test_p_values_use_three_significant_figures_and_small_ones_read_below_0_001(p, text):
    # Breaks if a tiny p prints as "p = 1e-05" or "p = 0.000", or a large one with six decimals.
    assert site.p_text(p) == text


def test_the_source_floor_is_shown_only_for_mcnemar_and_only_without_ties():
    # Breaks if the floor is printed for a Fisher entry, or for a source test that dropped ties
    # (then the sources tested are fewer than n_sources and 2 * 0.5^n_sources would be wrong).
    with_ties = mcnemar("violations", "drop", 1, 5, 0.21875, 4, 1, 1, 2, 0.625)
    out = lines(site.render(page_data(FISHER_V, MCNEMAR_V, with_ties)))

    assert "smallest p possible" not in out[0]
    assert "smallest p possible is 0.125" in out[1]
    assert "counting each of 4 sources once, p = 0.625." in out[2]
    assert "smallest p" not in out[2]


def test_the_floor_is_computed_from_the_source_count_and_capped_at_1():
    # Breaks if the floor is a constant or can exceed 1 (1 source: 2 * 0.5 = 1).
    one = mcnemar("violations", "drop", 1, 0, 1.0, 1, 0, 1, 0, 1.0)
    ten = mcnemar("violations", "drop", 0, 10, 0.001953, 10, 0, 0, 10, 0.001953)
    out = lines(site.render(page_data(one, ten)))

    assert "each of 1 source once, p = 1 (with 1 source the smallest p possible is 1)" in out[0]
    assert "smallest p possible is 0.00195)" in out[1]


def test_the_pooled_entry_appears_once_under_the_per_operator_lines():
    # Breaks if the pooled entry is repeated per operator or printed above the operators.
    drop = mcnemar("violations", "drop", 1, 5, 0.21875, 4, 0, 1, 3, 0.625)
    hedge = mcnemar("violations", "hedge", 0, 4, 0.125, 3, 0, 0, 3, 0.25)
    pooled = mcnemar("violations", "pooled", 1, 9, 0.021484, 4, 0, 1, 3, 0.625)
    out = lines(site.render(page_data(pooled, hedge, drop)))

    assert len(out) == 3
    assert "; drop)" in out[0] and "; hedge)" in out[1]
    assert sum("all operators pooled" in t for t in out) == 1
    assert "all operators pooled" in out[2]
    assert "p = 0.0215 over the pairs" in out[2]


def test_a_line_sits_directly_under_the_interval_line_for_the_same_outcome_and_operator():
    # Breaks if the attempts exact line is detached from the Newcombe line it belongs to.
    page = site.render(page_data(fisher("attempts", "drop", 0, 4, 1, 2, 0.333333)))
    block = group(page, "C-900")
    i = block.index("Gated minus block-only, runs that tried")
    j = block.index("Exact test (Fisher")

    assert i < j
    assert "<p" not in block[block.index("</p>", i) + 4 : j].replace('<p class="note exact">', "")


def test_compaction_setting_is_named_when_a_boundary_has_several():
    # Breaks if two lines for the same operator under different compaction settings look alike.
    data = page_with_two_settings()
    out = lines(site.render(data))

    assert any("compaction off" in t for t in out)
    assert any("compaction at 4000 tokens" in t for t in out)


def page_with_two_settings() -> dict:
    data = blocks_page()
    b = data["constraints"]["C-900"]["boundaries"][BID]
    for cell in list(b["cells"]):
        later = copy.deepcopy(cell)
        later["compaction"] = {"tokens": 4000, "keep": 3}
        b["cells"].append(later)
    off = {"tokens": None, "keep": None}
    for cell in b["cells"][:2]:
        cell["compaction"] = off
    on = {"tokens": 4000, "keep": 3}
    b["exact_tests"] = [
        fisher("violations", "drop", 0, 4, 1, 2, 0.333333, compaction=off),
        fisher("violations", "drop", 0, 4, 1, 2, 0.333333, compaction=on),
    ]
    return data


# --- absent, escaped, appendices ---------------------------------------------------------------


def test_without_exact_tests_the_page_has_no_exact_line_and_the_old_howto_box():
    # Breaks if an old results file gains text, or the reading guide grows without entries.
    page = site.render(blocks_page())
    box = re.search(r'<aside class="howto".*?</aside>', page, flags=re.S).group(0)

    assert "Exact test" not in page
    assert "exact test" not in box.lower()
    assert box.count("<li>") == 4


def test_the_exact_lines_are_the_only_new_text_in_the_main_part():
    # Breaks if adding entries changes anything but the lines and the reading box.
    plain = site.render(blocks_page())
    withx = site.render(page_data(FISHER_V))
    stripped = re.sub(r'<p class="note exact">.*?</p>', "", withx, flags=re.S)
    stripped = re.sub(r'<li class="exact-howto">.*?</li>', "", stripped, flags=re.S)

    assert stripped == plain


def test_the_how_to_read_box_explains_pairs_sources_and_many_comparisons():
    # Breaks if the paired test is shown without saying why pairs are matched, why the
    # per-source figure is the cautious one, and that no correction for many comparisons is made.
    page = site.render(page_data(MCNEMAR_V))
    box = text_of(re.search(r'<aside class="howto".*?</aside>', page, flags=re.S).group(0))

    assert "forks of the same sources" in box
    assert "matched pairs" in box
    assert "counts each source once is the cautious one" in box
    assert "smallest p it can reach is limited" in box
    assert "Many comparisons are shown on this page and none is corrected for that." in box


def test_the_page_never_says_significant():
    # Breaks if the page decides for the reader instead of reporting p.
    page = site.render(page_data(FISHER_V, MCNEMAR_V))

    assert "significan" not in text_of(page).lower()


def test_entries_are_escaped_and_the_page_stays_script_and_url_free():
    # Breaks if an operator or arm name from the file reaches the page unescaped.
    bad = fisher("violations", "<b>x</b>", 0, 4, 1, 2, 0.5, a="<i>g</i>")
    page = site.render(page_data(bad))

    assert "<b>x</b>" not in page and "<i>g</i>" not in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page
    assert "<script" not in page.lower()
    assert not re.search(r"(?:https?:)?//[a-z0-9]", page, flags=re.I)


def test_identical_input_gives_identical_bytes():
    # Breaks if the lines depend on anything but the input.
    assert site.render(page_data(MCNEMAR_V, FISHER_V)) == site.render(
        page_data(MCNEMAR_V, FISHER_V)
    )


def test_an_appendix_gets_the_lines_too():
    # Breaks if the held-out result sets show intervals but no exact test.
    main = blocks_page()
    appendix = site.Appendix("Second set", page_data(FISHER_V, MCNEMAR_V))
    page = site.render(main, appendices=[appendix])
    part = page[page.index("<!--appendices-->") :]

    assert part.count("Exact test (") == 2
    assert page[: page.index("<!--appendices-->")].count("Exact test (") == 0
    assert "Many comparisons are shown" in page


def test_the_numbers_on_the_page_equal_the_entries_of_a_stats_fixture():
    # Breaks if a printed count or p differs from the entry it came from.
    data = load("results_runs.json")
    entries = data["constraints"]["C-001"]["boundaries"][BID]["exact_tests"]
    out = lines(site.render(data), "C-001")

    assert len(out) == len(entries) == 3
    words = {
        "violations": "broke the rule",
        "attempts": "tried the forbidden action",
        "done": "did the job and kept the rule",
    }
    for entry in entries:
        (text,) = [t for t in out if words[entry["name"]] in t]
        assert f"{entry['k_a']} of {entry['n_a']} gated runs" in text
        assert f"{entry['k_b']} of {entry['n_b']} block-only runs" in text
        assert site.p_text(entry["p_exact"]) in text
