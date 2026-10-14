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
    assert "pooled into a cell 104" in text
    assert "not comparable 9" in text
    assert "rule never came up 7" in text
    assert "unreadable record 0" in text


def test_honesty_strip_says_not_measured_when_recall_is_null(rich):
    text = text_of(site.render(rich))
    assert "not measured extraction recall" in text
    assert "4 of 5 constraints have a compiled check" in text
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
    assert ">50%</text>" in page
    assert (
        "delegation:planner-&gt;coder: 15 of 30 mutated runs violated the rule "
        "(50%, 95% Wilson interval 33% to 67%), pooled over all operators and constraints. "
        "Clustered: includes forked runs that share a source run, so this interval is too narrow. "
        "Constraints: C-001, C-002." in page
    )


def test_edge_without_mutated_runs_falls_back_to_natural_and_says_so(rich):
    page = site.render(rich)
    assert ">return 0% natural</text>" in page
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
    assert "no compiled check" in text
    assert "Natural none 10 30% (11–60%) 3 of 10 runs 70% (40–89%) 7 of 10 runs" in text
    assert "Mutated drop 10 80% (49–94%) 8 of 10 runs 30% (11–60%) 3 of 10 runs" in text
    assert "compiled check" in text_of(article(page, "C-001"))


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
    assert re.search(r"Load-bearing score \(hedge\)[^+]*? not run", text)


def test_boundary_with_only_a_natural_cell_says_comparisons_were_not_run(rich):
    text = text_of(article(site.render(rich), "C-001"))
    assert "Load-bearing score and ledger value: not run (no mutated or gated runs" in text


def test_clustered_cell_carries_a_warning_once(rich):
    page = site.render(rich)
    assert page.count("<strong>Clustered.</strong>") == 1
    assert "from 4 sources" in text_of(article(page, "C-001"))
    assert "intervals are too narrow" in page


def test_comparison_crossing_zero_says_so_in_words(rich):
    page = site.render(rich)
    c2 = text_of(article(page, "C-002"))
    assert "Ledger value (drop)" in c2
    assert (
        "+10 pp (95% interval -30 pp to +50 pp) Not distinguishable from 0 at these sample sizes."
        in c2
    )
    assert page.count("Not distinguishable from 0 at these sample sizes.") == 1


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
        "NemoGate carried the rule across: task success 60%."
    ) in page
    assert (
        "broke it 50% of the time (5 of 10 runs). Blocking alone left task success at 50%. "
        "NemoGate carried the rule across: task success 100%."
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
    assert "No runs were pooled." in text
    assert "not measured extraction recall" in text
    # constraints_total counts the whole ledger even though no constraint has a pooled run
    assert "4 of 5 constraints have a compiled check" in text
    assert "16 inconclusive runs" in text
    assert "found 16" in text
    assert "pooled into a cell 0" in text
    assert "not comparable 9" in text
    # reproducibility is null when nothing was pooled
    assert "No runs were pooled, so there is no reproducibility group." in text
    assert "safe config none recorded" in text
    assert "models none recorded" in text
    assert '<svg class="graph"' not in page
    assert "<article" not in page


def test_unknown_format_version_is_refused(rich, tmp_path, capsys):
    rich["format_version"] = 2
    with pytest.raises(ValueError, match="format_version 2"):
        site.render(rich)
    src = tmp_path / "results.json"
    src.write_text(json.dumps(rich), encoding="utf-8")
    assert site.main(["--results", str(src), "--out", str(tmp_path / "out.html")]) == 2
    assert "format_version 2" in capsys.readouterr().err
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
        "NemoGate carried the rule across: task success 90%."
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
    warning = (
        "Clustered: includes forked runs that share a source run, so this interval is too narrow."
    )
    # the mutated drop cell of the first constraint is clustered, so the pooled edge is too
    assert (
        "(50%, 95% Wilson interval 33% to 67%), pooled over all operators and constraints. "
        f"{warning} Constraints: C-001, C-002." in page
    )
    row = re.search(r'<tr><th scope="row"><code>delegation:planner-&gt;coder</code>.*?</tr>', page)
    assert row
    assert warning in row.group(0)
    # the return boundary pools only an unclustered natural cell: no warning there
    ret = re.search(r'<tr><th scope="row"><code>return:coder-&gt;planner</code>.*?</tr>', page)
    assert ret
    assert "Clustered:" not in ret.group(0)


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
