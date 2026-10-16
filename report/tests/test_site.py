"""report/site.py: results.json to one static HTML page. Hand-written fixtures in fixtures/;
no model, no sandbox, no network."""

import copy
import html
import json
import re
from pathlib import Path

import pytest

from report import site

FIXTURES = Path(__file__).parent / "fixtures"
FORMAT_V1_DELEGATION = "delegation:planner->coder"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def rich() -> dict:
    data = load("results_rich.json")
    # C-002 is the fixture's rule without a check. These tests read rates off its card, so it is
    # graded here; the tests that need it ungraded say so (see `ungraded` below).
    data["constraints"]["C-002"]["has_check"] = True
    return data


@pytest.fixture
def ungraded() -> dict:
    """results_rich.json as written: C-002 has no compiled check although it has cells."""
    return load("results_rich.json")


def text_of(page: str) -> str:
    """The visible text: tags dropped, whitespace collapsed, entities decoded."""
    body = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return html.unescape(re.sub(r"\s+", " ", body))


def article(page: str, constraint_id: str) -> str:
    for block in re.findall(r'<article class="constraint".*?</article>', page, flags=re.S):
        if "<h3" in block and f"><code>{constraint_id}</code></h3>" in block:
            return block
    raise AssertionError(constraint_id)


def make_cell(arm, op, n, violations, successes):
    lo_hi = {"lo": 0.0, "hi": 1.0}
    return {
        "arm": arm,
        "operator": op,
        "n": n,
        "n_sources": n,
        "clustered": False,
        "violations": violations,
        "cvr": {"rate": violations / n, **lo_hi},
        "successes": successes,
        "success": {"rate": successes / n, **lo_hi},
        "cost_usd": 0.1,
    }


def one_constraint(boundaries: dict, text: str = "A rule.") -> dict:
    page = load("results_empty.json")
    page["constraints"] = {
        "C-900": {
            "text": text,
            "kind": "constraint",
            "has_check": True,
            "task_ids": [],
            "boundaries": boundaries,
        }
    }
    return page


# --- the arithmetic the page does itself -------------------------------------------------------


@pytest.mark.parametrize(
    ("k", "n", "lo", "hi"),
    [
        (0, 10, 0.0, 0.2775),
        (5, 10, 0.2366, 0.7634),
        (10, 10, 0.7225, 1.0),
        (15, 30, 0.3315, 0.6685),
    ],
)
def test_wilson_matches_hand_checked_anchors(k, n, lo, hi):
    got_lo, got_hi = site.wilson(k, n)
    assert (round(got_lo, 4), round(got_hi, 4)) == (lo, hi)


@pytest.mark.parametrize(
    ("value", "text"),
    [(0.125, "13%"), (0.1078, "11%"), (0.0, "0%"), (1.0, "100%"), (0.2775, "28%")],
)
def test_pct_rounds_halves_up(value, text):
    assert site.pct(value) == text


@pytest.mark.parametrize(
    ("value", "text"), [(0.5, "+50 pp"), (-0.3, "-30 pp"), (0.001, "0 pp"), (0.0, "0 pp")]
)
def test_pp_carries_the_sign_only_when_it_is_not_zero(value, text):
    assert site.pp(value) == text


# --- header and honesty strip ------------------------------------------------------------------


def test_header_shows_reproducibility_runs_and_honesty(rich):
    page = site.render(rich)
    text = text_of(page)
    assert "helper model/helper-x" in text
    assert "sut model/sut-y" in text
    assert "planner model/planner-z" in text
    assert "safe config 8c5a0a1ec16e" in text
    assert "repo commit 0123456789ab" in text  # 12 characters; the full sha is in the title
    assert 'title="0123456789abcdef0123456789abcdef01234567"' in page
    assert "found 120" in text
    assert "counted in the rates 104" in text
    assert "not comparable 9" in text
    assert "rule never came up 7" in text
    assert "unreadable record 0" in text


def test_honesty_strip_says_not_measured_when_recall_is_null(rich):
    text = text_of(site.render(rich))
    assert "not measured extraction recall" in text
    assert "4 of 5 rules can be graded: each has a compiled check" in text
    assert "16 inconclusive runs" in text


def test_honesty_strip_prints_a_measured_recall(rich):
    rich["honesty"]["extraction_recall"] = 0.75
    text = text_of(site.render(rich))
    assert "75% extraction recall" in text
    assert "not measured" not in text


# --- the graph ---------------------------------------------------------------------------------


def test_edge_is_pooled_mutated_rate_over_constraints_and_operators(rich):
    page = site.render(rich)
    # 5 + 2 + 8 violations in 30 mutated runs: 50%, Wilson 33% to 67%
    assert ">50% 15/30</text>" in page
    assert (
        "delegation:planner-&gt;coder: 15 of 30 mutated runs violated the rule "
        "(50%, 95% Wilson interval 33% to 67%), pooled over all operators and constraints. "
        "Clustered: includes forked runs that share a source run, so this interval is too narrow. "
        "Constraints: C-001, C-002." in page
    )


def test_edge_without_mutated_runs_falls_back_to_natural_and_says_so(rich):
    page = site.render(rich)
    assert ">return 0% 0/10 natural</text>" in page
    assert "return:coder-&gt;planner: 0 of 10 natural runs violated the rule" in page
    assert "Where a boundary has no mutated runs the natural runs are used" in page


def test_edge_with_neither_arm_is_no_data_not_zero():
    data = one_constraint(
        {"delegation:a->b": {"cells": [make_cell("gated", "drop", 10, 0, 10)], "comparisons": []}}
    )
    page = site.render(data)
    assert ">no data</text>" in page
    assert 'class="line stroke-nd dash"' in page
    assert "0%</text>" not in page


def test_edge_colour_bins_follow_the_rate():
    cells = [make_cell("mutated", "drop", 10, k, 5) for k in (0, 1, 3, 6)]
    boundaries = {
        f"delegation:a{i}->b{i}": {"cells": [cell], "comparisons": []}
        for i, cell in enumerate(cells)
    }
    page = site.render(one_constraint(boundaries))
    classes = re.findall(r'class="line stroke-(b\d)" stroke-width="[\d.]+" marker-end', page)
    assert classes == ["b0", "b1", "b2", "b3"]  # 0%, 10%, 30%, 60%: each on the bin's lower edge


def test_every_edge_links_to_a_card_that_exists(rich):
    page = site.render(rich)
    hrefs = re.findall(r'<a href="#([^"]+)"', page)
    assert hrefs
    for href in hrefs:
        assert f'id="{href}"' in page
    assert 'class="edge"' in page
    # the delegation edge goes to the first constraint that has that boundary
    assert re.search(r'<a href="#c0-c-001-b0" class="edge">', page)


def test_graph_nodes_come_from_the_data_and_follow_handoff_order():
    cell = [make_cell("mutated", "drop", 4, 1, 3)]
    data = one_constraint(
        {
            "delegation:alpha->beta": {"cells": cell, "comparisons": []},
            "delegation:beta->gamma": {"cells": cell, "comparisons": []},
        }
    )
    page = site.render(data)
    xs = {
        name: float(x)
        for x, name in re.findall(
            r'<text x="([\d.]+)" y="[\d.]+" text-anchor="middle" '
            r'class="nodename">([^<]+)</text>',
            page,
        )
    }
    assert set(xs) == {"alpha", "beta", "gamma"}
    assert xs["alpha"] < xs["beta"] < xs["gamma"]


def test_compaction_boundary_is_a_marker_on_its_agent_not_a_node(rich):
    rich["constraints"]["C-001"]["boundaries"]["compaction:planner"] = {
        "cells": [make_cell("mutated", "drop", 10, 2, 8)],
        "comparisons": [],
    }
    page = site.render(rich)
    assert "compaction 20%</text>" in page
    names = re.findall(r'class="nodename">([^<]+)</text>', page)
    assert sorted(names) == ["coder", "planner"]


def test_unrecognised_boundary_is_listed_but_not_drawn():
    data = one_constraint(
        {"mystery": {"cells": [make_cell("natural", "none", 4, 0, 4)], "comparisons": []}}
    )
    page = site.render(data)
    assert "<code>mystery</code> (not drawn)" in page
    assert '<svg class="graph"' not in page


# --- cards -------------------------------------------------------------------------------------


def test_card_shows_rule_kind_badge_and_cell_numbers(rich):
    page = site.render(rich)
    c2 = article(page, "C-002")
    text = text_of(c2)
    assert "scope" in text
    assert "Natural none 10 30% (11–60%) 3 of 10 runs 70% (40–89%) 7 of 10 runs" in text
    assert "Mutated drop 10 80% (49–94%) 8 of 10 runs 30% (11–60%) 3 of 10 runs" in text
    assert "Graded" in text_of(article(page, "C-001"))


def test_missing_cells_print_not_run_never_zero(rich):
    c1 = article(site.render(rich), "C-001")
    assert (
        'Block-only</th><td data-label="Operator">hedge</td>'
        '<td colspan="3" class="notrun">not run</td>'
    ) in c1
    assert (
        'Gated (NemoGate)</th><td data-label="Operator">hedge</td>'
        '<td colspan="3" class="notrun">not run</td>'
    ) in c1
    text = text_of(c1)
    assert re.search(
        r"Rule effect \(violations added by the sabotage\) \(hedge\)[^+]*? not run", text
    )


def test_boundary_with_only_a_natural_cell_says_comparisons_were_not_run(rich):
    text = text_of(article(site.render(rich), "C-001"))
    assert "Rule effect and gate effect: not run (no mutated or gated runs" in text


def test_clustered_cell_carries_a_warning_once(rich):
    page = site.render(rich)
    assert page.count("<strong>Not independent.</strong>") == 1
    assert "from 4 sources" in text_of(article(page, "C-001"))
    assert "ranges are too narrow" in page


def test_comparison_crossing_zero_says_so_in_words(rich):
    page = site.render(rich)
    c2 = text_of(article(page, "C-002"))
    assert "Gate effect on task success (drop)" in c2
    assert "+10 pp (95% interval -30 pp to +50 pp) Not distinguishable from 0 at n = 10." in c2
    assert page.count("Not distinguishable from 0 at n = 10.") == 1


def test_comparison_excluding_zero_says_so(rich):
    c1 = text_of(article(site.render(rich), "C-001"))
    assert "+50 pp (95% interval +10 pp to +70 pp) Higher than 0: the interval excludes 0." in c1
    assert "+50 pp (95% interval +20 pp to +70 pp) Higher than 0: the interval excludes 0." in c1


def test_negative_interval_is_reported_as_lower_than_zero(rich):
    comps = rich["constraints"]["C-002"]["boundaries"][FORMAT_V1_DELEGATION]["comparisons"]
    comps[0].update({"diff": -0.4, "lo": -0.6, "hi": -0.1})
    c2 = text_of(article(site.render(rich), "C-002"))
    assert "-40 pp (95% interval -60 pp to -10 pp) Lower than 0: the interval excludes 0." in c2


# --- the one-sentence summary ------------------------------------------------------------------


def test_summary_sentence_uses_only_numbers_from_the_cells(rich):
    page = site.render(rich)
    assert (
        "With the rule lost at the handoff <code>delegation:planner-&gt;coder</code>, the coder "
        "broke it 80% of the time (8 of 10 runs). Blocking alone left task success at 50%. "
        "With NemoGate carrying the rule, task success was 60%, +10 pp against blocking alone: "
        "not distinguishable from 0 at n = 10."
    ) in page
    assert (
        "broke it 50% of the time (5 of 10 runs). Blocking alone left task success at 50%. "
        "With NemoGate carrying the rule, task success was 100%, +50 pp against blocking alone "
        "(95% interval +20 pp to +70 pp)."
    ) in page


def test_summary_is_omitted_when_any_of_the_three_numbers_is_missing(rich):
    # C-001 has a mutated hedge cell but no block-only or gated hedge cell: no sentence for it
    page = site.render(rich)
    assert page.count('<p class="summary">') == 2
    assert "hedged" not in page
    cells = rich["constraints"]["C-002"]["boundaries"][FORMAT_V1_DELEGATION]["cells"]
    cells[:] = [c for c in cells if c["arm"] != "gated"]
    page = site.render(rich)
    assert page.count('<p class="summary">') == 1
    assert "task success 60%" not in page


@pytest.mark.parametrize("missing_arm", ["mutated", "block_only", "gated"])
def test_summary_needs_each_of_its_three_cells(rich, missing_arm):
    cells = rich["constraints"]["C-002"]["boundaries"][FORMAT_V1_DELEGATION]["cells"]
    cells[:] = [c for c in cells if c["arm"] != missing_arm]
    page = site.render(rich)
    assert "broke it 80% of the time" not in page
    assert page.count('<p class="summary">') == 1  # C-001's is unaffected


# --- verdict, key, method, graded and not graded ----------------------------------------------


def verdict_text(page: str) -> str:
    block = re.search(r'<p class="verdict-line">.*?</p>', page, flags=re.S)
    assert block, "no verdict line"
    return re.sub(r"\s+([,.:;)])", r"\1", text_of(block.group(0))).strip()


def raise_c002_effect(data: dict) -> None:
    """C-002's rule effect becomes +60 pp, larger than C-001's +50 pp."""
    comp = data["constraints"]["C-002"]["boundaries"][FORMAT_V1_DELEGATION]["comparisons"][0]
    comp.update({"diff": 0.6, "lo": 0.2, "hi": 0.9})


def test_verdict_states_the_largest_effect_with_its_runs_interval_and_the_gated_result(rich):
    # Breaks if the page opens without a plain result, or picks a smaller effect, or leaves out
    # the counts, the interval or what the gate did in the same case.
    raise_c002_effect(rich)

    assert verdict_text(site.render(rich)) == (
        "Largest effect found. At C-002 on delegation:planner->coder, with the rule lost, it was "
        "broken in 8 of 10 runs (80%) against 3 of 10 with no sabotage (30%): +60 pp (95% "
        "interval +20 pp to +90 pp). With the gate on, it was broken in 0 of 10 gated runs "
        "(0%, 95% interval 0% to 28%). That 0 comes from blocking: the block-only arm refuses "
        "the same action with the same check, so it gets 0 by construction and the figure does "
        "not show that carrying the rule helps."
    )


def test_verdict_ignores_a_rule_that_cannot_be_graded(ungraded):
    # Breaks if an ungraded rule's (meaningless) cells can win the headline.
    raise_c002_effect(ungraded)

    text = verdict_text(site.render(ungraded))

    assert text.startswith("Largest effect found. At C-001 on delegation:planner->coder")
    assert "+50 pp" in text and "C-002" not in text


def test_verdict_says_not_distinguishable_when_the_interval_includes_zero(rich):
    # Breaks if a win is claimed from an interval that includes 0.
    for cid in ("C-001", "C-002"):
        comp = rich["constraints"][cid]["boundaries"][FORMAT_V1_DELEGATION]["comparisons"][0]
        comp.update({"lo": -0.1})

    text = verdict_text(site.render(rich))

    assert "+50 pp (95% interval -10 pp to +70 pp, not distinguishable from 0 at n = 10)." in text


def test_verdict_prefers_an_effect_whose_interval_excludes_zero(rich):
    # Breaks if a bigger but unproven difference beats a smaller proven one.
    comp = rich["constraints"]["C-002"]["boundaries"][FORMAT_V1_DELEGATION]["comparisons"][0]
    comp.update({"diff": 0.9, "lo": -0.1, "hi": 1.0})

    text = verdict_text(site.render(rich))

    assert text.startswith("Largest effect found. At C-001 ")


def test_verdict_without_a_gated_run_says_there_is_none(rich):
    cells = rich["constraints"]["C-001"]["boundaries"][FORMAT_V1_DELEGATION]["cells"]
    cells[:] = [c for c in cells if c["arm"] != "gated"]

    assert "There are no gated runs of this case yet." in verdict_text(site.render(rich))


def test_verdict_with_nothing_to_compare_says_no_comparison_has_enough_runs():
    # Breaks if an empty page, or one with only natural runs, claims an effect.
    only_natural = one_constraint(
        {"delegation:a->b": {"cells": [make_cell("natural", "none", 4, 0, 4)], "comparisons": []}}
    )

    for page in (load("results_empty.json"), only_natural):
        assert verdict_text(site.render(page)).startswith("No comparison has enough runs yet.")


def test_key_defines_every_arm_and_operator_in_method_words():
    # Breaks if a judge meets "block-only" or "hedge" with no definition.
    text = text_of(site.render(load("results_empty.json")))
    key = text.split("Key", 1)[1].split("What these numbers can and cannot tell you")[0]

    for term in ("Natural", "Mutated", "Block-only", "Gated", "drop", "hedge", "compress_100"):
        assert term in key
    assert "the sentence that states the rule is removed from the handoff" in key
    assert "puts the rule back into the handoff" in key
    assert not re.search(r"\bT[1-8]\b|C-00\d", key)  # method words only, nothing about any task


def test_statistics_jargon_sits_in_the_collapsed_method_block(rich):
    # Breaks if a judge meets Wilson, Newcombe, pooled or clustered outside the Method block.
    page = site.render(rich)
    method = re.search(r'<details class="method">.*?</details>', page, flags=re.S).group(0)
    # tooltips (svg <title>) are hover detail, not page text
    visible = text_of(re.sub(r"<title>.*?</title>", "", page.replace(method, ""), flags=re.S))

    for word in ("Wilson", "Newcombe", "ooled", "lustered", "eproducibility"):
        assert word not in visible
    assert "Ranges show how uncertain each number is" in visible
    assert " open" not in method.split(">", 1)[0]
    assert "Newcombe hybrid-score interval" in method
    assert "reproducibility group" in method


def test_the_page_title_is_not_the_heading_again(rich):
    page = site.render(rich)

    assert page.count("NemoGate results") == 1  # the h1
    assert "<title>NemoGate: rule survival across handoffs</title>" in page


def test_cards_are_tagged_graded_or_not_graded(ungraded):
    page = site.render(ungraded)

    assert "Not graded" in text_of(article(page, "C-002"))
    assert "Not graded" not in text_of(article(page, "C-001"))
    assert "Graded" in text_of(article(page, "C-001"))


def test_a_rule_without_a_check_shows_no_rates_comparisons_or_trace(ungraded):
    # Breaks if the card of a rule the coverage panel calls ungradable still prints a violation
    # rate, a rule effect or a summary (the fixture has cells for it: 40 runs).
    page = site.render(ungraded)
    card = article(page, "C-002")
    text = text_of(card)

    assert "Not graded." in text
    assert "40 runs reached this handoff" in text
    for shown in ("80%", "30%", "Rule effect", "Gate effect", "of 10 runs", "excludes 0"):
        assert shown not in text
    assert '<table class="cells"' not in card
    assert 'class="summary"' not in card
    assert 'id="c1-c-002-b0"' in card  # the graph and the table still have somewhere to link


def test_an_ungraded_rule_does_not_feed_the_boundary_rate(ungraded):
    # Breaks if C-002's violation counts (always meaningless without a check) are pooled into
    # the edge: with them it reads 15/30; without, only C-001's 5 + 2 of 20 mutated runs count.
    page = site.render(ungraded)

    assert ">35% 7/20</text>" in page
    assert "7 of 20 mutated runs violated the rule" in page
    assert "Constraints: C-001, C-002." in page


def test_a_boundary_seen_only_by_an_ungraded_rule_is_no_data_and_still_linked():
    data = one_constraint(
        {"delegation:a->b": {"cells": [make_cell("mutated", "drop", 10, 0, 10)], "comparisons": []}}
    )
    data["constraints"]["C-900"]["has_check"] = False

    page = site.render(data)

    assert ">no data</text>" in page
    assert "no data (no graded constraint here)" in text_of(page)
    href = re.search(r'<a href="#([^"]+)" class="edge">', page).group(1)
    assert f'id="{href}"' in page


def test_scrolling_tables_and_the_graph_are_focusable_named_and_hinted(rich):
    # Breaks if a table that scrolls sideways at 375 px cannot be reached from the keyboard, or
    # the wide graph gives no sign that it scrolls.
    page = site.render(rich)
    wraps = re.findall(r'<div class="tablewrap"[^>]*>', page)

    assert len(wraps) == 5  # coverage, boundary table, and one per boundary section (3)
    for wrap in wraps:
        assert 'tabindex="0"' in wrap and 'role="region"' in wrap and 'aria-label="' in wrap
    assert 'aria-label="Runs of C-001 at delegation:planner-&gt;coder"' in page
    assert "This graph is wider than the screen: scroll sideways." in page


# --- safety and determinism --------------------------------------------------------------------


def test_every_dynamic_string_is_escaped(rich):
    rich["constraints"]["C-001"]["boundaries"]["delegation:a<i>->b&c"] = {
        "cells": [make_cell("natural", "none", 4, 1, 3)],
        "comparisons": [],
    }
    rich["constraints"]["C-001"]["kind"] = "<b>kind</b>"
    rich["reproducibility"]["model_ids"]["sut"] = '"><img src=x>'
    rich["constraints"]["C-002"]["task_ids"] = ["<u>t</u>"]
    page = site.render(rich)
    assert "<script" not in page
    assert "<img" not in page
    assert "<i>" not in page and "<b>kind" not in page and "<u>" not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; friends." in page
    assert "delegation:a&lt;i&gt;-&gt;b&amp;c" in page
    assert "&quot;&gt;&lt;img src=x&gt;" in page


def test_page_is_self_contained(rich):
    page = site.render(rich)
    assert "<script" not in page
    assert not re.search(r'(?:src|href)="(?:https?:)?//', page)
    assert "@import" not in page
    assert not re.search(r"url\((?!#)", page)  # only in-page marker references
    assert page.startswith("<!doctype html>")
    assert 'name="color-scheme"' in page
    assert "prefers-color-scheme:dark" in page


def test_two_renders_are_byte_identical(rich, tmp_path):
    assert site.render(rich) == site.render(copy.deepcopy(rich))
    src = FIXTURES / "results_rich.json"
    assert site.main(["--results", str(src), "--out", str(tmp_path / "a" / "index.html")]) == 0
    assert site.main(["--results", str(src), "--out", str(tmp_path / "b" / "index.html")]) == 0
    assert (tmp_path / "a" / "index.html").read_bytes() == (
        tmp_path / "b" / "index.html"
    ).read_bytes()


# --- empty and wrong inputs --------------------------------------------------------------------


def test_zero_runs_page_still_shows_the_honesty_strip_and_no_graph():
    page = site.render(load("results_empty.json"))
    text = text_of(page)
    assert "No runs were counted." in text
    assert "not measured extraction recall" in text
    # constraints_total counts the whole ledger even though no constraint has a pooled run
    assert "4 of 5 rules can be graded: each has a compiled check" in text
    assert "16 inconclusive runs" in text
    assert "found 16" in text
    assert "counted in the rates 0" in text
    assert "not comparable 9" in text
    # reproducibility is null when nothing was pooled
    assert "No runs were counted, so there is no shared set of conditions." in text
    assert "safe config none recorded" in text
    assert "models none recorded" in text
    assert '<svg class="graph"' not in page
    assert "<article" not in page


def test_unknown_format_version_is_refused(rich, tmp_path, capsys):
    rich["format_version"] = 3
    with pytest.raises(ValueError, match="format_version 3"):
        site.render(rich)
    src = tmp_path / "results.json"
    src.write_text(json.dumps(rich), encoding="utf-8")
    assert site.main(["--results", str(src), "--out", str(tmp_path / "out.html")]) == 2
    assert "format_version 3" in capsys.readouterr().err
    assert not (tmp_path / "out.html").exists()


def test_missing_results_file_is_a_clean_error(tmp_path, capsys):
    code = site.main(["--results", str(tmp_path / "nope.json"), "--out", str(tmp_path / "o.html")])
    assert code == 2
    assert "report.site:" in capsys.readouterr().err


# --- review fixes ------------------------------------------------------------------------------


def test_summary_names_the_recipient_only_on_a_delegation(rich):
    # a return boundary may be graded on the sender's reply, so the recipient must not be blamed
    cells = [
        make_cell("mutated", "drop", 10, 5, 5),
        make_cell("block_only", "drop", 10, 0, 4),
        make_cell("gated", "drop", 10, 0, 9),
    ]
    rich["constraints"]["C-001"]["boundaries"]["return:coder->planner"]["cells"] = cells
    page = site.render(rich)
    assert (
        "With the rule lost at the handoff <code>return:coder-&gt;planner</code>, the rule was "
        "broken 50% of the time (5 of 10 runs). Blocking alone left task success at 40%. "
        "With NemoGate carrying the rule, task success was 90%."
    ) in page
    assert "the planner broke it" not in page
    assert "the coder broke it" in page  # the delegation sentence still names its recipient


def node_rects(page: str) -> dict[str, tuple[float, float, float, float]]:
    rects = {}
    for x, y, w, h, name in re.findall(
        r'<g class="node"><rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)" '
        r'rx="8"/><text [^>]*class="nodename">([^<]+)</text>',
        page,
    ):
        rects[name] = (float(x), float(y), float(x) + float(w), float(y) + float(h))
    return rects


def edge_paths(page: str) -> dict[str, list[tuple[float, float]]]:
    """boundary id -> sampled points of its drawn path (M, then L or Q)."""
    out = {}
    for bid, d in re.findall(
        r'<title>([^:<]+:[^:<]+?): [^<]*</title><path d="([^"]+)" class="hit"/>', page
    ):
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]
        pts = [(nums[0], nums[1])]
        if "Q" in d:
            (x1, y1), (cx, cy), (x2, y2) = nums[0:2], nums[2:4], nums[4:6]
            for i in range(1, 101):
                t = i / 100
                pts.append(
                    (
                        (1 - t) ** 2 * x1 + 2 * t * (1 - t) * cx + t * t * x2,
                        (1 - t) ** 2 * y1 + 2 * t * (1 - t) * cy + t * t * y2,
                    )
                )
        else:
            pts.append((nums[2], nums[3]))
        out[html.unescape(bid)] = pts
    return out


def test_same_layer_edge_is_a_real_arc_and_no_edge_crosses_a_node():
    cell = [make_cell("mutated", "drop", 4, 1, 3)]
    data = one_constraint(
        {
            "delegation:a->c": {"cells": cell, "comparisons": []},
            "delegation:b->a": {"cells": cell, "comparisons": []},
            "delegation:c->b": {"cells": cell, "comparisons": []},
        }
    )
    page = site.render(data)
    rects = node_rects(page)
    assert set(rects) == {"a", "b", "c"}
    assert rects["b"][0] == rects["c"][0]  # b and c share a column: the case that used to collapse
    paths = edge_paths(page)
    assert set(paths) == {"delegation:a->c", "delegation:b->a", "delegation:c->b"}
    cb = re.search(
        r'delegation:c-&gt;b: [^<]*</title><path d="M(-?[\d.]+) (-?[\d.]+)Q(-?[\d.]+) (-?[\d.]+) '
        r'(-?[\d.]+) (-?[\d.]+)"',
        page,
    )
    assert cb
    x1, _, cx, _, x2, _ = (float(v) for v in cb.groups())
    assert cx not in (x1, x2)  # not a vertical sliver that doubles back through the nodes
    for bid, pts in paths.items():
        for name, (left, top, right, bottom) in rects.items():
            inside = [p for p in pts if left + 1 < p[0] < right - 1 and top + 1 < p[1] < bottom - 1]
            assert not inside, (bid, name, inside[:2])


def test_clustered_pooled_rate_is_flagged_in_tooltip_and_table(rich):
    page = site.render(rich)
    tooltip = (
        "Clustered: includes forked runs that share a source run, so this interval is too narrow."
    )
    warning = (
        "Not independent: includes forked runs that share a source run, "
        "so this range is too narrow."
    )
    # the mutated drop cell of the first constraint is clustered, so the pooled edge is too
    assert (
        "(50%, 95% Wilson interval 33% to 67%), pooled over all operators and constraints. "
        f"{tooltip} Constraints: C-001, C-002." in page
    )
    row = re.search(r'<tr><th scope="row"><code>delegation:planner-&gt;coder</code>.*?</tr>', page)
    assert row
    assert warning in row.group(0)
    # the return boundary pools only an unclustered natural cell: no warning there
    ret = re.search(r'<tr><th scope="row"><code>return:coder-&gt;planner</code>.*?</tr>', page)
    assert ret
    assert "Not independent" not in ret.group(0)


def test_unwritable_output_is_a_clean_error(tmp_path, capsys):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    src = FIXTURES / "results_rich.json"
    code = site.main(["--results", str(src), "--out", str(blocker / "sub" / "index.html")])
    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("report.site: ")
    assert err.count("\n") == 1
    assert site.main(["--results", str(src), "--out", "/dev/null/index.html"]) == 2
    assert capsys.readouterr().err.startswith("report.site: ")


# --- natural violations split by carriage label --------------------------------------------------


def natural_split(page: dict, split: dict | None) -> dict:
    """`rich` with C-002's natural cell (3 violations in 10 runs) carrying `split`, if any."""
    out = copy.deepcopy(page)
    for cell in out["constraints"]["C-002"]["boundaries"]["delegation:planner->coder"]["cells"]:
        if cell["arm"] == "natural":
            assert cell["violations"] == 3
            if split is not None:
                cell["violations_by_label"] = split
    return out


def test_natural_cell_says_how_many_violations_were_dropped_and_how_many_disobeyed(rich):
    # Breaks if a dropped rule and a disobeyed one are shown as one undifferentiated rate again.
    page = site.render(natural_split(rich, {"DROPPED": 1, "DISOBEYED": 2}))
    text = text_of(article(page, "C-002"))
    assert "3 of 10 broken: 1 lost in the handoff, 2 carried but disobeyed" in text


def test_natural_split_names_a_run_that_could_not_be_classified(rich):
    # Breaks if a violation with no usable label vanishes from the sentence, so the parts no
    # longer add up to the violation count.
    page = site.render(natural_split(rich, {"DROPPED": 1, "unlabelled": 1, "UNKNOWN": 1}))
    text = text_of(article(page, "C-002"))
    assert "3 of 10 broken: 1 lost in the handoff, 2 not classified" in text


def test_natural_split_is_only_shown_in_the_natural_row(rich):
    # Breaks if the sentence is repeated on a sabotaged arm, where "dropped" is by design.
    data = natural_split(rich, {"DROPPED": 3})
    for cell in data["constraints"]["C-002"]["boundaries"]["delegation:planner->coder"]["cells"]:
        if cell["arm"] == "mutated":
            cell["violations_by_label"] = {"DROPPED": 8}
    text = text_of(article(site.render(data), "C-002"))
    assert text.count("broken:") == 1


@pytest.mark.parametrize("split", [None, {}])
def test_natural_cell_without_a_split_renders_as_before(rich, split):
    # Breaks if an older results file (no field) fails to render or shows an empty sentence.
    text = text_of(article(site.render(natural_split(rich, split)), "C-002"))
    assert "broken:" not in text
    assert "Natural none 10 30% (11–60%) 3 of 10 runs 70% (40–89%) 7 of 10 runs" in text


# --- what each part fixes -----------------------------------------------------------------------


def test_page_says_what_the_gate_and_repair_each_fix_and_what_neither_does(rich):
    # Breaks if the page claims more than the code does: the gate blocks only checks decidable
    # before the call, repair acts on rules lost in the handoff, frozen files are graded after.
    page = site.render(rich)
    text = text_of(page)
    assert "What each part fixes" in text
    section = text.split("What each part fixes", 1)[1].split("What these numbers can")[0]
    assert "blocks a tool call that breaks a rule it can check before the call" in section
    assert "path, tool and approval rules" in section
    assert "rule that arrived but was ignored" in section
    assert "Gated also holds back a reply that misses an output rule" in section
    assert "Repair targets rules lost in the handoff" in section
    assert "checked on the files an agent changed, such as frozen files," in section
    assert "are graded after the run. The gate does not block them" in section
    assert not re.search(r"\bT[1-8]\b|C-00\d", section)


# --- cells split by the compaction knobs -------------------------------------------------------


def test_cells_made_under_different_compaction_settings_each_get_a_row():
    # Breaks if the table finds a cell by (arm, operator) alone: the compacted natural runs
    # (2 of 4 broken) would be hidden behind the uncompacted ones (0 of 4), with no sign that
    # a second setting exists.
    off = {**make_cell("natural", "none", 4, 0, 4), "compaction": {"tokens": None, "keep": None}}
    on = {**make_cell("natural", "none", 4, 2, 2), "compaction": {"tokens": 6000, "keep": 6}}
    page = site.render(
        one_constraint({"delegation:planner->coder": {"cells": [off, on], "comparisons": []}})
    )

    text = text_of(article(page, "C-900"))
    assert "none compaction off" in text
    assert "none compaction at 6000 tokens, keep 6" in text
    assert "different compaction settings, so each setting has its own rows" in text


def test_a_single_compaction_setting_adds_nothing_to_the_table():
    # Breaks if every table starts carrying a compaction label when nothing was split.
    off = {**make_cell("natural", "none", 4, 0, 4), "compaction": {"tokens": None, "keep": None}}
    page = site.render(
        one_constraint({"delegation:planner->coder": {"cells": [off], "comparisons": []}})
    )

    text = text_of(article(page, "C-900"))
    assert "compaction" not in text


OFF = {"tokens": None, "keep": None}
ON = {"tokens": 6000, "keep": 6}


def with_setting(cell, setting):
    return {**cell, "compaction": setting}


def ledger_comparison(setting, diff, lo, hi, op="drop"):
    return {
        "name": "ledger_value",
        "operator": op,
        "compaction": setting,
        "a": {"arm": "gated", "operator": op},
        "b": {"arm": "block_only", "operator": op},
        "metric": "success",
        "diff": diff,
        "lo": lo,
        "hi": hi,
    }


def mixed_settings_boundary():
    """Mutated runs exist only with compaction on; block-only and gated runs under both."""
    cells = [
        with_setting(make_cell("mutated", "drop", 10, 8, 5), ON),
        with_setting(make_cell("block_only", "drop", 10, 0, 3), OFF),
        with_setting(make_cell("block_only", "drop", 10, 0, 1), ON),
        with_setting(make_cell("gated", "drop", 10, 0, 9), OFF),
        with_setting(make_cell("gated", "drop", 10, 0, 5), ON),
    ]
    comparisons = [
        ledger_comparison(OFF, 0.6, 0.17, 0.81),
        ledger_comparison(ON, 0.4, -0.1, 0.68),
    ]
    return {"delegation:planner->coder": {"cells": cells, "comparisons": comparisons}}


def test_the_summary_takes_rates_and_difference_from_the_same_compaction_setting():
    # Breaks if the gate effect is the first ledger_value comparison for the operator: the
    # compaction-on rates (block-only 10%, gated 50%) would be printed next to the
    # compaction-off difference (+60 pp, interval excluding 0) instead of their own +40 pp,
    # whose interval crosses 0.
    page = site.render(one_constraint(mixed_settings_boundary()))

    assert (
        "Blocking alone left task success at 10%. With NemoGate carrying the rule, task success "
        "was 50%, +40 pp against blocking alone: not distinguishable from 0 at n = 10."
    ) in text_of(page)
    assert "+60 pp against blocking alone" not in text_of(page)


def test_the_summary_names_the_setting_when_the_boundary_has_several():
    # Breaks if a sentence about compacted runs does not say it is about compacted runs.
    text = text_of(site.render(one_constraint(mixed_settings_boundary())))
    assert "at the handoff delegation:planner->coder under compaction at 6000 tokens, keep 6," in (
        text
    )


def test_each_compaction_setting_gets_its_own_effect_comparisons():
    # Breaks if the effect lines show one setting's comparison for the whole boundary: the first
    # comparison per operator would be the off one (+60 pp), and nothing would say so.
    text = text_of(article(site.render(one_constraint(mixed_settings_boundary())), "C-900"))

    off, on = text.index("Under compaction off"), text.index("Under compaction at 6000 tokens")
    assert off < text.index("+60 pp (95% interval +17 pp to +81 pp)") < on
    assert on < text.index("+40 pp (95% interval -10 pp to +68 pp)")
    assert "first setting listed" not in text


def test_a_setting_with_only_natural_runs_has_no_effect_block():
    # Breaks if a setting that has no mutated or gated cells is shown a "not run" effect line next
    # to the setting that was actually compared, which reads as if the comparison failed there.
    cells = [
        with_setting(make_cell("natural", "none", 10, 2, 8), ON),
        with_setting(make_cell("mutated", "drop", 10, 8, 5), OFF),
        with_setting(make_cell("block_only", "drop", 10, 0, 3), OFF),
        with_setting(make_cell("gated", "drop", 10, 0, 9), OFF),
    ]
    boundary = {
        "delegation:planner->coder": {
            "cells": cells,
            "comparisons": [ledger_comparison(OFF, 0.6, 0.17, 0.81)],
        }
    }
    text = text_of(article(site.render(one_constraint(boundary)), "C-900"))

    assert "Under compaction off" in text
    assert "Under compaction at 6000 tokens" not in text
    assert "+60 pp" in text


def test_natural_runs_where_the_rule_never_came_up_are_counted_on_the_row():
    # Breaks if the page shows "1 of 10 broken" with nothing to say the rule only came up in 4
    # of the 10 runs: the other 6 are in the rate's denominator.
    cell = {
        **make_cell("natural", "none", 10, 1, 9),
        "violations_by_label": {"DROPPED": 1},
        "not_exercised": 6,
    }
    page = site.render(
        one_constraint({"delegation:planner->coder": {"cells": [cell], "comparisons": []}})
    )

    text = text_of(article(page, "C-900"))
    assert "1 of 10 broken: 1 lost in the handoff" in text
    assert "the rule never came up in 6 of 10 runs" in text


def test_a_natural_row_with_no_unexercised_runs_says_nothing_about_it():
    # Breaks if every natural row carries a "never came up" line, including those where it did.
    cell = {**make_cell("natural", "none", 10, 1, 9), "not_exercised": 0}
    page = site.render(
        one_constraint({"delegation:planner->coder": {"cells": [cell], "comparisons": []}})
    )
    assert "never came up" not in text_of(article(page, "C-900"))


def test_the_header_says_never_exercised_runs_are_not_left_out(rich):
    # Breaks if "left out of every rate" is left unqualified now that natural runs where the
    # rule never came up stay in the rates.
    text = text_of(site.render(rich))
    assert "inconclusive runs, left out of every rate below" in text
    assert "Natural runs where the rule never came up are not among them" in text
