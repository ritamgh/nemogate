"""report/site.py: the exact tests. `replay.stats` writes an optional `exact_tests` list per
boundary; the page prints one table per boundary, a row per entry, under the gated-minus-block-only
interval lines, taking every number and arm name from the entry. Fixture entries are hand-built;
every expected number below was checked by hand (McNemar: 2 * P(X <= 1 | n = 6, 1/2) = 14/64)."""

import copy
import re

import pytest

from report import site
from report.tests.test_site import load, text_of, without_toc
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


def tables(page: str, cid: str = "C-900") -> list[str]:
    """Every exact-test table of one rule's group, in page order."""
    return re.findall(r'<table class="blocks exact">.*?</table>', group(page, cid), flags=re.S)


def rows(page: str, cid: str = "C-900") -> list[list[str]]:
    """The body rows of the rule's exact-test table as the text of their seven cells, in page
    order: outcome, operator, gated, block-only, test, p over runs or pairs, p by source."""
    (table,) = tables(page, cid)
    body = table.split("<tbody>")[1]
    return [
        [text_of(c).strip() for c in re.findall(r"<t[hd][ >].*?</t[hd]>", row, flags=re.S)]
        for row in re.findall(r"<tr>.*?</tr>", body, flags=re.S)
    ]


def footnote(page: str, cid: str = "C-900") -> str:
    """The text of the paragraphs under the exact-test table."""
    block = group(page, cid)
    table = tables(page, cid)[0]
    after = block[block.index(table) + len(table) :]
    return " ".join(
        text_of(p).strip()
        for p in re.findall(r'<p class="note exact">(.*?)</p>', after, flags=re.S)
    )


DASH = "\u2014"


# --- the table ----------------------------------------------------------------------------------


def test_a_fisher_row_has_the_outcome_both_counts_and_p_from_the_entry():
    # Breaks if the independent-runs row is hand-typed, drops a count, swaps the arms or shows a
    # by-source figure a Fisher test does not have.
    out = rows(site.render(page_data(FISHER_V)))

    assert out == [
        [
            "Broke the rule",
            "drop",
            "0 of 4 runs",
            "1 of 2 runs",
            "Fisher, independent runs",
            "0.333",
            DASH,
        ]
    ]


def test_a_mcnemar_row_has_the_pairs_both_p_values_and_the_sources():
    # Breaks if the paired row shows only one p, mixes up whose count is whose, or leaves out the
    # sources the cautious figure counts (8 pairs: 5 only block-only, 1 only gated; 4 sources).
    out = rows(site.render(page_data(MCNEMAR_V)))

    assert out == [
        [
            "Broke the rule",
            "drop",
            "1 of 8 pairs (only this arm)",
            "5 of 8 pairs (only this arm)",
            "McNemar, matched forks of the same sources",
            "0.219",
            "0.625 (4 sources)",
        ]
    ]


def test_each_boundary_has_one_table_however_many_entries_it_has():
    # Breaks if the entries are printed as separate tables or paragraphs again.
    page = site.render(page_data(FISHER_V, MCNEMAR_V, fisher("done", "drop", 4, 4, 1, 2, 0.06)))

    assert len(tables(page)) == 1
    assert len(rows(page)) == 3
    assert 'class="tablewrap"' in group(page, "C-900")
    assert 'class="note exact">Exact test' not in page


def test_the_table_head_names_its_seven_columns():
    # Breaks if a column is unnamed, or a figure sits under the wrong name.
    (table,) = tables(site.render(page_data(FISHER_V)))
    head = re.findall(r'<th scope="col">(.*?)</th>', table)

    assert head == [
        "Outcome",
        "Operator",
        "Gated",
        "Block-only",
        "Test",
        "p over runs or pairs",
        "p counting each source once",
    ]


def test_swapping_a_and_b_swaps_who_is_who_in_both_kinds_of_row():
    # Breaks if the code assumes `a` is gated: the entry names its own arms, so the counts of
    # the gated arm stay in the Gated column when it is `b`.
    swapped_f = fisher("violations", "drop", 1, 2, 0, 4, 0.333333, a="block_only", b="gated")
    swapped_m = mcnemar(
        "violations", "drop", 5, 1, 0.21875, 4, 0, 3, 1, 0.625, a="block_only", b="gated"
    )
    out = rows(site.render(page_data(swapped_f, swapped_m)))

    assert out[0][2:4] == ["0 of 4 runs", "1 of 2 runs"]
    assert out[1][2:4] == ["1 of 8 pairs (only this arm)", "5 of 8 pairs (only this arm)"]


def test_outcome_words_for_attempts_and_done():
    # Breaks if an outcome is described with another outcome's words.
    page = site.render(
        page_data(
            fisher("attempts", "drop", 0, 4, 1, 2, 0.333333),
            fisher("done", "drop", 4, 4, 1, 2, 0.060606),
        )
    )
    out = rows(page)

    assert out[0][0] == "Tried the forbidden action" and out[0][2:4] == [
        "0 of 4 runs",
        "1 of 2 runs",
    ]
    assert out[1][0] == "Did the job and kept the rule" and out[1][2:4] == [
        "4 of 4 runs",
        "1 of 2 runs",
    ]
    assert out[1][5] == "0.0606"


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


def test_a_tiny_p_reads_below_0_001_in_both_p_columns():
    # Breaks if a p under 0.001 prints as "0" or "1e-05" in the table, or the cautious column
    # keeps the exact figure.
    tiny = mcnemar("violations", "drop", 0, 12, 0.000488, 12, 0, 0, 12, 0.000488)
    (row,) = rows(site.render(page_data(tiny)))

    assert row[5:] == ["< 0.001", "< 0.001 (12 sources)"]


def test_the_source_floor_is_noted_only_for_mcnemar_and_only_without_ties():
    # Breaks if the floor is printed for a Fisher entry alone, or for a source test that dropped
    # ties (then the sources tested are fewer than n_sources and 2 * 0.5^n_sources would be wrong).
    with_ties = mcnemar("violations", "hedge", 1, 5, 0.21875, 4, 1, 1, 2, 0.625)
    only_fisher = site.render(page_data(FISHER_V))
    paired = site.render(page_data(FISHER_V, MCNEMAR_V, with_ties))

    assert "smallest p" not in footnote(only_fisher)
    assert footnote(paired).count("the smallest p possible is") == 1
    assert "with 4 sources the smallest p possible is 0.125" in footnote(paired)
    assert rows(paired)[2][6] == "0.625 (4 sources)"  # the tied entry keeps its figure, no floor


def test_the_floor_is_computed_from_the_source_count_and_capped_at_1():
    # Breaks if the floor is a constant or can exceed 1 (1 source: 2 * 0.5 = 1).
    one = mcnemar("violations", "drop", 1, 0, 1.0, 1, 0, 1, 0, 1.0)
    ten = mcnemar("violations", "hedge", 0, 10, 0.001953, 10, 0, 0, 10, 0.001953)
    page = site.render(page_data(one, ten))

    assert "with 1 source the smallest p possible is 1;" in footnote(page)
    assert "with 10 sources the smallest p possible is 0.00195" in footnote(page)
    assert rows(page)[0][6] == "1 (1 source)"


def test_the_pooled_entry_is_one_row_named_for_what_it_pools_and_comes_last():
    # Breaks if the pooled entry is repeated per operator or printed above the operators.
    drop = mcnemar("violations", "drop", 1, 5, 0.21875, 4, 0, 1, 3, 0.625)
    hedge = mcnemar("violations", "hedge", 0, 4, 0.125, 3, 0, 0, 3, 0.25)
    pooled = mcnemar("violations", "pooled", 1, 9, 0.021484, 4, 0, 1, 3, 0.625)
    out = rows(site.render(page_data(pooled, hedge, drop)))

    assert [r[1] for r in out] == ["drop", "hedge", "Pooled over operators"]
    assert out[2][5] == "0.0215"


def test_the_table_follows_the_interval_line_and_holds_the_attempts_row_too():
    # Breaks if the exact tests are detached from the interval lines of their boundary, or the
    # attempts entry (once printed under its interval line) is lost from the table.
    page = site.render(page_data(fisher("attempts", "drop", 0, 4, 1, 2, 0.333333)))
    block = group(page, "C-900")
    i = block.index("Gated minus block-only, runs that tried")

    assert i < block.index('<table class="blocks exact">')
    assert rows(page)[0][0] == "Tried the forbidden action"


def test_compaction_setting_is_named_when_a_boundary_has_several():
    # Breaks if two rows for the same operator under different compaction settings look alike.
    data = page_with_two_settings()
    out = [r[1] for r in rows(site.render(data))]

    assert out == ["drop compaction off", "drop compaction at 4000 tokens, keep 3"]


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


def test_the_exact_table_is_the_only_new_text_in_the_main_part():
    # Breaks if adding entries changes anything but the table and its footnote, the headline
    # block at the top (which needs the entries) and the reading box.
    plain = site.render(blocks_page())
    withx = site.render(page_data(FISHER_V))
    stripped = re.sub(
        r'<div class="tablewrap"[^>]*><table class="blocks exact">.*?</table></div>',
        "",
        withx,
        flags=re.S,
    )
    stripped = re.sub(r'<p class="note exact">.*?</p>', "", stripped, flags=re.S)
    stripped = re.sub(r'<section class="ogroup glance".*?</section>', "", stripped, flags=re.S)
    stripped = re.sub(r'<li class="exact-howto">.*?</li>', "", stripped, flags=re.S)

    assert without_toc(stripped) == without_toc(plain)


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
    # arms that are not gated and block-only are printed with their own names, escaped
    assert rows(page)[0][2:4] == ["<i>g</i>: 0 of 4 runs", "block-only: 1 of 2 runs"]
    assert "<script" not in page.lower()
    assert not re.search(r"(?:https?:)?//[a-z0-9]", page, flags=re.I)


def test_identical_input_gives_identical_bytes():
    # Breaks if the lines depend on anything but the input.
    assert site.render(page_data(MCNEMAR_V, FISHER_V)) == site.render(
        page_data(MCNEMAR_V, FISHER_V)
    )


def test_an_appendix_gets_the_table_too():
    # Breaks if the held-out result sets show intervals but no exact test.
    main = blocks_page()
    appendix = site.Appendix("Second set", page_data(FISHER_V, MCNEMAR_V))
    page = site.render(main, appendices=[appendix])
    part = page[page.index("<!--appendices-->") :]

    assert part.count('<table class="blocks exact">') == 1
    assert (
        len(re.findall(r"<tr>", part.split('<table class="blocks exact">')[1].split("</table>")[0]))
        == 3
    )
    assert '<table class="blocks exact">' not in page[: page.index("<!--appendices-->")]
    assert "Many comparisons are shown" in page


def test_the_numbers_on_the_page_equal_the_entries_of_a_stats_fixture():
    # Breaks if a printed count or p differs from the entry it came from.
    data = load("results_runs.json")
    entries = data["constraints"]["C-001"]["boundaries"][BID]["exact_tests"]
    out = rows(site.render(data), "C-001")

    assert len(out) == len(entries) == 3
    words = {
        "violations": "Broke the rule",
        "attempts": "Tried the forbidden action",
        "done": "Did the job and kept the rule",
    }
    for entry in entries:
        (row,) = [r for r in out if r[0] == words[entry["name"]]]
        assert row[2] == f"{entry['k_a']} of {entry['n_a']} runs"
        assert row[3] == f"{entry['k_b']} of {entry['n_b']} runs"
        assert row[5] == site.p_figure(entry["p_exact"])


def test_the_no_sabotage_operator_reads_as_words():
    # Breaks if the natural-source operator prints as the bare word "none".
    out = rows(site.render(page_data(fisher("violations", "none", 0, 4, 1, 2, 0.333333))))

    assert out[0][1] == "no sabotage"
