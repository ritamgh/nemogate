"""report/site.py: the "Cost of prevention" table under "At a glance". Cells are built by hand and
every expected figure is worked by hand in the comment above it."""

import re

from report import site
from report.tests.test_cost import cell
from report.tests.test_site import FIXTURES, one_constraint, text_of
from report.tests.test_site_appendix import appendix_part, main_part, write_json
from report.tests.test_site_exact_tests import BID, fisher

DASH = "—"
ARROW = "→"


def cells_for_drop() -> list[dict]:
    # ungated 8/10 (80%, 55% to 95%), gated 2/10 (20%, 5% to 45%), block-only 1/10 (10%).
    # extra cost gated 0.16 - 0.10 = 0.06, block-only 0.15 - 0.10 = 0.05; prevented 0.6, 0.7.
    # price gated 0.06 / 0.6 = 0.10, block-only 0.05 / 0.7 = 0.0714 -> 0.071;
    # widest reading gated 0.01 / 0.90 = 0.0111 -> 0.011 to 0.11 / 0.10 = 1.10 -> 1.100
    return [
        cell("mutated", "drop", 10, 8, 0.10, 0.08, 0.12, cvr=(0.55, 0.95)),
        cell("gated", "drop", 10, 2, 0.16, 0.13, 0.19, cvr=(0.05, 0.45)),
        cell("block_only", "drop", 10, 1, 0.15, 0.12, 0.18, cvr=(0.02, 0.40)),
    ]


def page(cells=None, exact=True, text="A rule.") -> dict:
    data = one_constraint(
        {BID: {"cells": cells if cells is not None else cells_for_drop(), "comparisons": []}},
        text=text,
    )
    if exact:
        data["constraints"]["C-900"]["boundaries"][BID]["exact_tests"] = [
            fisher("violations", "drop", 2, 10, 1, 10, 1.0)
        ]
    return data


def section(html: str) -> str:
    return re.search(r'<section class="ogroup cost".*?</section>', html, flags=re.S).group(0)


def rows(html: str) -> list[list[str]]:
    table = re.search(r"<table.*?</table>", section(html), flags=re.S).group(0)
    body = table.split("<tbody>")[1]
    return [
        [text_of(c).strip() for c in re.findall(r"<t[hd][ >].*?</t[hd]>", r, flags=re.S)]
        for r in re.findall(r"<tr>.*?</tr>", body, flags=re.S)
    ]


def test_one_row_per_arm_with_the_figures_worked_by_hand():
    # Breaks if a figure is taken from the wrong cell, the arms swap, or a price is not extra cost
    # over prevented per run (see the worked figures above).
    gated, block = rows(site.render(page()))

    assert gated[0].startswith("A rule.")
    assert gated[1:4] == [
        "Gated",
        f"8 of 10 (80%) {ARROW} 2 of 10 (20%) 95% intervals 55% to 95%, 5% to 45%",
        "60 widest reading: 10 to 90",
    ]
    assert gated[4] == "$0.0600 more per run widest reading: $0.0100 more to $0.1100 more"
    assert gated[5] == "about $0.100 widest reading: $0.011 to $1.100"
    assert gated[6] == "Whole runs"
    assert block[1] == "Block-only"
    assert block[3] == "70 widest reading: 15 to 93"
    assert block[5].startswith("about $0.071 ")


def test_the_table_sits_after_at_a_glance_and_before_how_to_read_this():
    # Breaks if the table is not directly under the glance block.
    html = site.render(page())

    assert html.index('class="ogroup glance"') < html.index('class="ogroup cost"')
    assert html.index('class="ogroup cost"') < html.index('<aside class="howto"')
    assert "Cost of prevention" in text_of(section(html))


def test_a_gate_that_cost_less_is_said_in_words_and_never_shown_as_a_negative_price():
    # Breaks if a negative extra cost reaches the page as "-$0.02" or as a price. Worked:
    # gated 0.08 against 0.10 is 0.02 less per run.
    cells = cells_for_drop()
    cells[1] = cell("gated", "drop", 10, 2, 0.08, 0.06, 0.10, cvr=(0.05, 0.45))
    gated, _ = rows(site.render(page(cells)))

    assert gated[4].startswith("Gated cost $0.0200 less per run")
    assert gated[5] == "— gated cost less per run"
    text = text_of(section(site.render(page(cells))))
    assert "-$" not in text
    assert "$-" not in text


def test_no_violations_prevented_is_a_dash_with_the_reason():
    # Breaks if a row with nothing prevented shows a price or leaves the dash unexplained.
    cells = cells_for_drop()
    cells[0] = cell("mutated", "drop", 10, 2, 0.10, 0.08, 0.12)
    cells[1] = cell("gated", "drop", 10, 2, 0.16, 0.13, 0.19)
    gated, _ = rows(site.render(page(cells)))

    assert gated[5] == "— no violations prevented here"
    assert gated[3].startswith("0 ")


def test_fewer_than_five_runs_and_a_range_are_labelled_not_hidden():
    # Breaks if a 4-run cell is shown without its label or its cost range passes as an interval.
    cells = cells_for_drop()
    cells[0] = cell("mutated", "drop", 4, 3, 0.10, 0.08, 0.12, cvr=(0.3, 0.95), basis="range")
    cells[1] = cell("gated", "drop", 4, 0, 0.16, 0.13, 0.19, cvr=(0.0, 0.5), basis="range")
    gated, _ = rows(site.render(page(cells)))

    assert gated[6] == "Whole runs fewer than 5 runs cost is a range, not a 95% interval"
    assert gated[5] == "about $0.080 no interval for the price"
    assert "widest reading" not in gated[4]


def test_forks_are_labelled_as_covering_only_what_ran_after_the_fork_point():
    # Breaks if a fork's partial cost is shown as a whole-run cost.
    cells = cells_for_drop()
    for c in cells:
        c["n_forks"] = 10
    gated, _ = rows(site.render(page(cells)))

    assert gated[6] == "After the fork point only"


def test_whole_runs_against_forks_get_no_row_and_the_page_says_how_many_were_left_out():
    # Breaks if the natural source run's whole cost is set against a fork's partial cost, or
    # if the rows left out go unmentioned. Two comparisons (gated, block-only) are left out.
    cells = [
        cell("natural", "none", 10, 6, 0.50, 0.40, 0.60, cvr=(0.3, 0.8)),
        cell("gated", "none", 10, 0, 0.12, 0.10, 0.14, cvr=(0.0, 0.3), forks=10),
        cell("block_only", "none", 10, 0, 0.13, 0.10, 0.15, cvr=(0.0, 0.3), forks=10),
    ]
    html = site.render(page(cells))
    text = text_of(section(html))

    assert "<table" not in section(html)
    assert "2 comparisons left out" in text
    assert "Their rates are in the table above." in text


def test_the_left_out_line_comes_with_the_rows_that_were_priced():
    # Breaks if the note is lost when some rows are priced and some are not.
    data = page()
    data["constraints"]["C-900"]["boundaries"][BID]["cells"] += [
        cell("natural", "none", 10, 6, 0.50, 0.40, 0.60, cvr=(0.3, 0.8)),
        cell("gated", "none", 10, 0, 0.12, 0.10, 0.14, cvr=(0.0, 0.3), forks=10),
    ]
    html = site.render(data)

    assert len(rows(html)) == 2
    assert "1 comparison left out" in text_of(section(html))


def test_an_older_results_file_gets_no_table_and_the_goldens_are_the_bytes_they_were(tmp_path):
    # Breaks if a results file without exact tests or without arm_metrics gains a section.
    assert "Cost of prevention" not in site.render(page(exact=False))

    bare = page()
    for c in bare["constraints"]["C-900"]["boundaries"][BID]["cells"]:
        del c["arm_metrics"]
    assert "Cost of prevention" not in site.render(bare)

    for name in ("results_rich", "results_empty"):
        out = tmp_path / f"{name}.html"
        assert site.main(["--results", str(FIXTURES / f"{name}.json"), "--out", str(out)]) == 0
        assert out.read_bytes() == (FIXTURES / f"golden_{name}.html").read_bytes()


def test_the_caption_says_what_the_price_is_and_is_not_and_never_says_significant():
    # Breaks if the table can be read as a price list or as a verdict.
    text = text_of(section(site.render(page())))

    assert "a price on this reference app and these rules, not a price list" in text
    assert "The rates and the prices come from the same runs." in text
    assert "Read the detail below before relying on one row." in text
    assert "whole runs or only what ran after the handoff" in text
    assert "significan" not in text.lower()


def test_rule_text_operator_and_arm_names_from_the_file_are_escaped():
    # Breaks if wording from the results file reaches the page unescaped.
    cells = cells_for_drop()
    for c in cells:
        c["operator"] = "<i>x</i>"
    cells[1]["arm"] = "gated"
    data = page(cells, text="<script>alert(1)</script>")
    html = site.render(data)

    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in section(html)
    assert "<i>x</i>" not in html
    assert "&lt;i&gt;x&lt;/i&gt;" in section(html)


def test_the_section_adds_no_script_url_or_style():
    # Breaks if the table brings anything but markup and the existing table classes.
    part = section(site.render(page()))

    assert "<script" not in part
    assert "http" not in part
    assert "<style" not in part
    assert 'class="outcome cost"' in part


def test_a_row_links_to_the_boundary_section_of_its_rule_and_the_target_exists():
    # Breaks if the rule's cell links nowhere.
    html = site.render(page())
    href = re.search(r'<a href="#([^"]+)"', section(html)).group(1)

    assert f'id="{href}"' in html


def test_each_appendix_gets_its_own_table_with_the_rule_wording_withheld(tmp_path):
    # Breaks if the appendix rows join the main table, show the private wording, or link to the
    # main page's ids.
    main = page(text="Public wording.")
    app = page(text="PRIVATE-WORDING-ZZ")
    argv = ["--results", write_json(tmp_path / "m.json", main), "--out", str(tmp_path / "o.html")]
    argv += ["--appendix", "Second run", write_json(tmp_path / "a.json", app)]
    assert site.main(argv) == 0
    html = (tmp_path / "o.html").read_text(encoding="utf-8")
    part = appendix_part(html)

    assert "Public wording." in section(main_part(html))
    assert main_part(html).count('class="ogroup cost"') == 1
    assert part.count('class="ogroup cost"') == 1
    assert "PRIVATE-WORDING-ZZ" not in html
    assert "Rule text withheld (held-out task)" in text_of(section(part))
    assert 'href="#a1-c0-c-900-b0"' in section(part)
    assert 'id="appendix-1-cost-h"' in part


def test_the_hook_adds_the_section_and_changes_nothing_else(monkeypatch):
    # Breaks if the hook moves, drops or rewrites any other part of the page.
    html = site.render(page())
    monkeypatch.setattr(site, "render_cost", lambda *args, **kwargs: "")

    assert html.replace(section(html), "") == site.render(page())
