"""report/site.py: the "At a glance" block that opens the page. One table per results set, one
row per graded rule and condition (sabotage, handoff, compaction setting), the rates of the
ungated, block-only and gated arms and the exact test p, all read from the results file. Cells
are built by hand, so every expected number is checked by hand."""

import copy
import re

from report import site
from report.tests.test_site import load, make_cell, one_constraint, text_of
from report.tests.test_site_appendix import appendix_part, main_part, write_json
from report.tests.test_site_arm_metrics import rate, with_metrics
from report.tests.test_site_exact_tests import BID, fisher, mcnemar

DASH = "—"


def cells_for_drop() -> list[dict]:
    # hand-checked: 4/10 = 40%, 3/8 = 37.5 -> 38%, 1/8 = 12.5 -> 13%
    return [
        make_cell("natural", "none", 10, 5, 6),
        make_cell("mutated", "drop", 10, 4, 5),
        make_cell("block_only", "drop", 8, 3, 4),
        make_cell("gated", "drop", 8, 1, 7),
    ]


def page(cells=None, exact=None, text="A rule.") -> dict:
    data = one_constraint(
        {BID: {"cells": cells if cells is not None else cells_for_drop(), "comparisons": []}},
        text=text,
    )
    if exact is None:
        exact = [fisher("violations", "drop", 1, 8, 3, 8, 0.2821)]
    data["constraints"]["C-900"]["boundaries"][BID]["exact_tests"] = exact
    return data


def glance(html: str) -> str:
    return re.search(r'<section class="ogroup glance".*?</section>', html, flags=re.S).group(0)


def glance_rows(html: str) -> list[list[str]]:
    """Each body row of the first glance table as its cells' text: head, then the four columns."""
    table = re.search(r"<table.*?</table>", glance(html), flags=re.S).group(0)
    body = table.split("<tbody>")[1]
    rows = re.findall(r"<tr>.*?</tr>", body, flags=re.S)
    return [
        [text_of(c).strip() for c in re.findall(r"<t[hd][ >].*?</t[hd]>", r, flags=re.S)]
        for r in rows
    ]


def test_a_row_reads_its_three_rates_from_the_ungated_block_only_and_gated_cells():
    # Breaks if "no gate" is read off the natural cell instead of the sabotaged one at the same
    # operator, if block-only and gated are swapped, or if a rate is not k of n of its own cell.
    (row,) = glance_rows(site.render(page()))

    assert row[0].startswith("A rule.")
    assert row[1:] == ["4 of 10 (40%)", "3 of 8 (38%)", "1 of 8 (13%)", "0.282"]


def test_the_block_opens_the_page_above_how_to_read_this():
    # Breaks if the result is not the first thing on the page.
    html = site.render(page())

    assert html.index('class="ogroup glance"') < html.index('<aside class="howto"')
    assert html.index("<h1>") < html.index('class="ogroup glance"')
    assert "At a glance" in text_of(glance(html))


def test_each_sabotage_setting_has_its_own_row_and_none_uses_the_natural_cell():
    # Breaks if rates of different conditions are added together (the no-gate arm at "no
    # sabotage" is natural, at "drop" it is mutated) or if a condition is left out.
    cells = cells_for_drop() + [
        make_cell("block_only", "none", 6, 2, 3),
        make_cell("gated", "none", 6, 0, 6),
    ]
    exact = [
        fisher("violations", "drop", 1, 8, 3, 8, 0.2821),
        fisher("violations", "none", 0, 6, 2, 6, 0.4545),
        fisher("violations", "pooled", 1, 14, 5, 14, 0.1),
    ]
    rows = glance_rows(site.render(page(cells, exact)))

    assert [r[0].split(" ", 2)[-1] for r in rows] == ["no sabotage", "rule lost (drop)"]
    assert rows[0][1:] == ["5 of 10 (50%)", "2 of 6 (33%)", "0 of 6 (0%)", "0.455"]
    assert rows[1][1:] == ["4 of 10 (40%)", "3 of 8 (38%)", "1 of 8 (13%)", "0.282"]


def test_a_missing_cell_or_test_shows_a_dash_never_zero():
    # Breaks if a column with no data reads "0 of 0" or "0%" (a zero would claim a measurement).
    cells = [c for c in cells_for_drop() if c["arm"] != "mutated"]
    (row,) = glance_rows(
        site.render(page(cells, exact=[fisher("attempts", "drop", 0, 8, 1, 8, 0.5)]))
    )

    assert row[1:5] == [DASH, "3 of 8 (38%)", "1 of 8 (13%)", DASH]


def test_the_p_column_is_the_violations_entry_of_the_rows_own_operator_only():
    # Breaks if the p of another outcome (attempts) or another operator is shown on the row.
    exact = [
        fisher("attempts", "drop", 0, 8, 1, 8, 0.0123),
        fisher("violations", "hedge", 0, 8, 1, 8, 0.9),
    ]
    (row,) = glance_rows(site.render(page(exact=exact)))

    assert row[4] == DASH


def test_a_paired_test_shows_both_p_values_the_pairs_one_and_the_cautious_one():
    # Breaks if the by-source p (the cautious one) is left out of the headline: 0.219 over 8
    # pairs against 0.625 counting 4 sources once.
    exact = [mcnemar("violations", "drop", 1, 5, 0.21875, 4, 0, 1, 3, 0.625)]
    (row,) = glance_rows(site.render(page(exact=exact)))

    assert row[4] == "0.219 0.625 counting each source once"


def test_an_entry_that_is_not_gated_against_block_only_is_not_shown_as_that_test():
    # Breaks if the column header's claim (gated vs block-only) is filled from another pair of arms.
    exact = [fisher("violations", "drop", 1, 8, 3, 8, 0.2821, a="natural", b="mutated")]
    (row,) = glance_rows(site.render(page(exact=exact)))

    assert row[4] == DASH


def test_every_graded_rule_has_a_row_and_an_ungraded_rule_has_none():
    # Breaks if a rule is left out because it has no gated or block-only run, or if a rule
    # without a compiled check (no run can be graded) gets rates.
    data = page()
    only_natural = copy.deepcopy(data["constraints"]["C-900"])
    only_natural["boundaries"][BID]["cells"] = [make_cell("natural", "none", 5, 1, 4)]
    only_natural["boundaries"][BID]["exact_tests"] = []
    only_natural["text"] = "Second rule."
    ungraded = copy.deepcopy(only_natural)
    ungraded["has_check"] = False
    ungraded["text"] = "Third rule."
    data["constraints"]["C-901"] = only_natural
    data["constraints"]["C-902"] = ungraded
    rows = glance_rows(site.render(data))

    assert len(rows) == 2
    assert rows[0][0].startswith("A rule.") and rows[1][0].startswith("Second rule.")
    assert rows[1][1:] == [DASH, DASH, DASH, DASH]
    assert "Third rule." not in text_of(glance(site.render(data)))


def test_a_row_links_to_the_boundary_section_of_its_rule_and_the_target_exists():
    # Breaks if the link points at an id the page does not have.
    html = site.render(page())
    href = re.search(r'<a href="#([^"]+)"', glance(html)).group(1)

    assert href == "c0-c-900-b0"
    assert f'id="{href}"' in html


def test_the_caption_says_these_are_all_rules_and_never_says_significant():
    # Breaks if the table can be read as a selection, or as a verdict on one row.
    text = text_of(glance(site.render(page())))

    assert "These are all the graded rules that were tested, not a selection." in text
    assert "Each rate is the runs that broke the rule over the runs made." in text
    assert "Read the detail below before relying on a single row." in text
    assert "significan" not in text.lower()


def test_no_exact_tests_means_no_block_and_the_goldens_stay_the_bytes_they_were():
    # Breaks if an older results file (no exact_tests) gains a block at the top.
    old = page(exact=[])

    assert "glance" not in site.render(old)
    assert "At a glance" not in site.render(load("results_rich.json"))


def test_a_rule_text_with_markup_is_escaped():
    # Breaks if rule wording from the file reaches the headline unescaped.
    html = site.render(page(text="<script>alert(1)</script>"))

    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in glance(html)


def test_a_boundary_and_a_compaction_setting_are_named_when_there_are_several():
    # Breaks if two rows of one rule (two handoffs, two compaction settings) look alike.
    data = page()
    b = data["constraints"]["C-900"]["boundaries"]
    b["return:coder->planner"] = copy.deepcopy(b[BID])
    off, on = {"tokens": None, "keep": None}, {"tokens": 4000, "keep": 3}
    for cell in b["return:coder->planner"]["cells"]:
        cell["compaction"] = off
    for cell in b[BID]["cells"]:
        cell["compaction"] = off
    later = copy.deepcopy(b[BID]["cells"])
    for cell in later:
        cell["compaction"] = on
    b[BID]["cells"] += later
    rows = [r[0] for r in glance_rows(site.render(data))]

    assert len(rows) == 3
    assert any("delegation:planner->coder" in r and "compaction off" in r for r in rows)
    assert any("delegation:planner->coder" in r and "compaction at 4000 tokens" in r for r in rows)
    assert any("return:coder->planner" in r for r in rows)


def test_each_appendix_gets_its_own_table_and_withholds_the_rule_wording(tmp_path):
    # Breaks if the appendix rows are merged into the main table, show the private wording, or
    # link to the main page's ids.
    main = page(text="Public wording.")
    app = page(text="PRIVATE-WORDING-ZZ")
    argv = ["--results", write_json(tmp_path / "m.json", main), "--out", str(tmp_path / "o.html")]
    argv += ["--appendix", "Second run", write_json(tmp_path / "a.json", app)]
    assert site.main(argv) == 0
    html = (tmp_path / "o.html").read_text(encoding="utf-8")
    part = appendix_part(html)

    assert "Public wording." in glance(main_part(html))
    assert main_part(html).count('class="ogroup glance"') == 1
    assert part.count('class="ogroup glance"') == 1
    assert "PRIVATE-WORDING-ZZ" not in html
    assert "Rule text withheld (held-out task)" in text_of(glance(part))
    assert 'href="#a1-c0-c-900-b0"' in glance(part)


ARROW = "\u2192"
ATTEMPT_HEAD = "Tried the forbidden action, block-only " + ARROW + " gated"


def attempt_cells() -> list[dict]:
    # hand-checked: 4/8 = 50%, 1/8 = 12.5 -> 13%
    cells = cells_for_drop()
    for cell in cells:
        if cell["arm"] == "block_only":
            with_metrics(cell, attempt_rate=rate(4, 8, 0.5, 0.2, 0.8))
        if cell["arm"] == "gated":
            with_metrics(cell, attempt_rate=rate(1, 8, 0.125, 0.02, 0.47))
    return cells


def attempt_exact(p=0.2821, k=(1, 4)) -> list[dict]:
    return [
        fisher("violations", "drop", 1, 8, 3, 8, 0.2821),
        fisher("attempts", "drop", k[0], 8, k[1], 8, p),
    ]


def head_labels(html: str) -> list[str]:
    return re.findall(r'<th scope="col">(.*?)</th>', glance(html))


def test_zero_against_zero_gets_no_p_and_the_caption_says_why():
    # Breaks if a test of 0 broke against 0 broke (block-only gets 0 by construction) prints
    # p = 1, which reads as a result, or the caption does not say that such a pair is untested.
    zero = fisher("violations", "drop", 0, 8, 0, 8, 1.0)
    html = site.render(page(exact=[zero]))
    (row,) = glance_rows(html)

    assert row[4] == DASH
    assert (
        "Where block-only refuses the same action it gets 0 by construction, so a 0 against 0 "
        "is not tested." in text_of(glance(html))
    )


def test_zero_against_zero_shows_no_by_source_line_either_for_a_paired_test():
    # Breaks if the cautious figure is left under the dash of a paired 0 against 0 entry.
    zero = {**mcnemar("violations", "drop", 0, 0, 1.0, 4, 0, 0, 0, 1.0), "k_a": 0, "k_b": 0}
    (row,) = glance_rows(site.render(page(exact=[zero])))

    assert row[4] == DASH


def test_one_arm_with_a_count_keeps_its_p():
    # Breaks if the zero rule is applied when only one arm is 0 (0 against 1 is a result).
    (row,) = glance_rows(site.render(page(exact=[fisher("violations", "drop", 0, 8, 1, 8, 0.5)])))

    assert row[4] == "0.5"


def test_the_attempts_column_gives_each_arms_count_and_the_attempts_p():
    # Breaks if the block hides that the gate cuts attempts at the forbidden action, or reads
    # the counts from another cell than the detail table (4 of 8 block-only, 1 of 8 gated).
    html = site.render(page(attempt_cells(), attempt_exact()))
    (row,) = glance_rows(html)

    assert head_labels(html)[5:] == [ATTEMPT_HEAD, "Exact test p (tried the forbidden action)"]
    assert row[5:] == ["4 of 8 (50%) " + ARROW + " 1 of 8 (13%)", "0.282"]
    assert "4 of 8 runs, blocked or not" in text_of(html)  # the detail table's figure
    assert "1 of 8 runs, blocked or not" in text_of(html)


def test_the_attempts_p_is_the_attempts_entry_of_the_rows_own_operator_and_has_no_zero_pair():
    # Breaks if the attempts p comes from the violations entry or another operator, or a 0
    # against 0 attempts pair prints p = 1.
    other = fisher("attempts", "hedge", 0, 8, 4, 8, 0.01)
    (row,) = glance_rows(site.render(page(attempt_cells(), [attempt_exact()[0], other])))
    assert row[6] == DASH

    zero = [attempt_exact()[0], fisher("attempts", "drop", 0, 8, 0, 8, 1.0)]
    (row,) = glance_rows(site.render(page(attempt_cells(), zero)))
    assert row[6] == DASH


def test_a_missing_attempt_count_is_a_dash_never_zero():
    # Breaks if a cell without attempt data reads 0 of 0, or hides the other arm's count.
    cells = attempt_cells()
    for cell in cells:
        if cell["arm"] == "gated":
            del cell["arm_metrics"]
    (row,) = glance_rows(site.render(page(cells, attempt_exact())))
    assert row[5] == "4 of 8 (50%) " + ARROW + " " + DASH

    for cell in cells:
        if cell["arm"] == "block_only":
            cell["arm_metrics"]["attempt_rate"] = None  # a rule not about a tool or path
    (row,) = glance_rows(site.render(page(cells, attempt_exact())))
    assert row[5] == DASH


def test_without_attempts_entries_the_set_has_no_attempts_column():
    # Breaks if an older file (exact tests but none for attempts) gains two empty columns.
    html = site.render(page(attempt_cells(), [fisher("violations", "drop", 1, 8, 3, 8, 0.2821)]))

    assert len(head_labels(html)) == 5
    assert ATTEMPT_HEAD not in html
    assert len(glance_rows(html)[0]) == 5


def test_each_set_decides_for_itself_whether_it_has_the_attempts_column(tmp_path):
    # Breaks if an appendix with attempts entries drags the column into the main table, or the
    # reverse.
    main = page(attempt_cells(), [fisher("violations", "drop", 1, 8, 3, 8, 0.2821)])
    app = page(attempt_cells(), attempt_exact())
    argv = ["--results", write_json(tmp_path / "m.json", main), "--out", str(tmp_path / "o.html")]
    argv += ["--appendix", "Second run", write_json(tmp_path / "a.json", app)]
    assert site.main(argv) == 0
    html = (tmp_path / "o.html").read_text(encoding="utf-8")

    assert ATTEMPT_HEAD not in glance(main_part(html))
    assert ATTEMPT_HEAD in glance(appendix_part(html))
