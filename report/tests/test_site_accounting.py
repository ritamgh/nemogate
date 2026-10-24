"""The "Run accounting" tables of an appendix: found, counted and not counted, and the counted runs
by rule, sabotage setting and arm, every figure computed from the appendix's own results file.
Fixture data here is made up (results_rich.json is hand-written)."""

import re

from report import site
from report.tests.test_site import text_of
from report.tests.test_site_appendix import appendix_data, appendix_part, build

DASH = "—"


def sections(page: str) -> list[str]:
    """The accounting section of each appendix, in order ('' for an appendix without one)."""
    parts = re.split(r'(?=<section class="appendix" id="appendix-)', appendix_part(page))[1:]
    out = []
    for part in parts:
        found = re.search(r'<section class="ogroup accounting".*?</section>', part, flags=re.S)
        out.append(found.group(0) if found else "")
    return out


def rows(section: str) -> list[tuple[str, list[str]]]:
    """(row label, cell texts) for every body row of every table of the section."""
    out = []
    for tr in re.findall(r"<tbody>.*?</tbody>", section, flags=re.S):
        for row in re.findall(r"<tr>(.*?)</tr>", tr, flags=re.S):
            label = re.search(r'<th scope="row">(.*?)</th>', row, flags=re.S)
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)
            out.append((text_of(label.group(1)).strip(), [text_of(c).strip() for c in cells]))
    return out


def one_set(tmp_path, data):
    return sections(build(tmp_path, [("Set one", data, None)]))[0]


def test_the_totals_are_the_files_found_counted_and_the_difference(tmp_path):
    # Breaks if a total is taken from another set, recomputed from the cells (which hold 90 runs
    # here, not 104) or if "not counted" is not found minus counted. Literals: 9999 - 104 = 9895.
    section = one_set(tmp_path, appendix_data())

    assert rows(section)[:3] == [
        ("Run folders found", ["9999"]),
        ("Finished and counted in the rates", ["104"]),
        ("Found but not counted", ["9895"]),
    ]


def test_the_counted_runs_are_summed_by_rule_operator_and_arm(tmp_path):
    # Breaks if a cell is attributed to the wrong rule, operator or arm, if an arm that has no
    # cell shows 0 instead of a dash, or if boundaries are not added (C-001 has two natural
    # cells of 10, so its no-sabotage natural figure is 20). Hand-checked from results_rich.json.
    section = one_set(tmp_path, appendix_data())

    assert rows(section)[3:] == [
        ("C-001 no sabotage", ["20", DASH, DASH, DASH, "20"]),
        ("C-001 rule lost (drop)", [DASH, "10", "10", "10", "30"]),
        ("C-001 rule made vague (hedge)", [DASH, "10", DASH, DASH, "10"]),
        ("C-002 no sabotage", ["10", DASH, DASH, DASH, "10"]),
        ("C-002 rule lost (drop)", [DASH, "10", "10", "10", "30"]),
    ]


def test_the_columns_are_the_four_arms_and_the_total(tmp_path):
    # Breaks if a column is renamed or reordered so a number sits under the wrong arm.
    section = one_set(tmp_path, appendix_data())

    head = re.findall(r"<thead>.*?</thead>", section, flags=re.S)[1]
    assert [text_of(h).strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", head)] == [
        "Rule and sabotage setting",
        "Natural",
        "Mutated",
        "Block-only",
        "Gated (NemoGate)",
        "All arms",
    ]


def test_a_missing_found_or_counted_is_a_dash_and_never_0(tmp_path):
    # Breaks if a missing field is read as 0 (the old header printed 0 for a missing `found`),
    # which would also make "not counted" a negative or a made-up number.
    no_found = appendix_data()
    del no_found["runs"]["found"]
    no_pooled = appendix_data()
    del no_pooled["runs"]["pooled"]

    assert rows(one_set(tmp_path, no_found))[:3] == [
        ("Run folders found", [DASH]),
        ("Finished and counted in the rates", ["104"]),
        ("Found but not counted", [DASH]),
    ]
    assert rows(one_set(tmp_path, no_pooled))[:3] == [
        ("Run folders found", ["9999"]),
        ("Finished and counted in the rates", [DASH]),
        ("Found but not counted", [DASH]),
    ]


def test_found_below_counted_is_not_turned_into_a_negative(tmp_path):
    # Breaks if an inconsistent file prints a negative "not counted" instead of a dash.
    data = appendix_data()
    data["runs"]["found"] = 50

    assert rows(one_set(tmp_path, data))[2] == ("Found but not counted", [DASH])


def test_a_file_with_none_of_the_fields_gets_no_table_and_the_old_page(tmp_path, monkeypatch):
    # Breaks if an old results file (no run counts, no cells) gains an empty table, or if the
    # table changes anything else of the page: with the table switched off the bytes are the same.
    data = appendix_data()
    data["runs"] = {}
    data["constraints"] = {}
    page = build(tmp_path, [("Set one", data, None)])
    assert sections(page) == [""]
    assert "Run accounting" not in page

    full = appendix_data()
    with_table = build(tmp_path, [("Set one", full, None)], name="with.html")
    monkeypatch.setattr(site, "run_accounting", lambda *args, **kwargs: "")
    without_table = build(tmp_path, [("Set one", full, None)], name="without.html")
    assert with_table != without_table
    assert (
        with_table.replace(
            re.search(
                r'<section class="ogroup accounting".*?</section>', with_table, flags=re.S
            ).group(0),
            "",
        )
        == without_table
    )


def test_each_set_shows_only_its_own_numbers(tmp_path):
    # Breaks if a figure of another set leaks in: the second set has 7777 found and 31 counted,
    # the first 9999 and 104, and neither section may show the other's.
    second = appendix_data()
    second["runs"]["found"] = 7777
    second["runs"]["pooled"] = 31
    first, other = sections(
        build(tmp_path, [("Set one", appendix_data(), None), ("Set two", second, None)])
    )

    assert "9999" in text_of(first) and "7777" not in text_of(first)
    assert "7777" in text_of(other) and "9999" not in text_of(other)
    assert "104" not in text_of(other) and "31" not in text_of(first)
    assert 'id="appendix-1-accounting-h"' in first and 'id="appendix-2-accounting-h"' in other


def test_the_main_page_gets_no_accounting_table(tmp_path):
    # Breaks if the table leaks into the main part, which an appendix never changes.
    page = build(tmp_path, [("Set one", appendix_data(), None)])

    assert "Run accounting" not in page[: page.index("<!--appendices-->")]


def test_rule_ids_and_operators_are_escaped(tmp_path):
    # Breaks if a rule id or an operator name from the file reaches the page as markup.
    data = appendix_data()
    body = data["constraints"].pop("C-002")
    data["constraints"]["<b>Z</b>"] = body
    cells = body["boundaries"]["delegation:planner->coder"]["cells"]
    cells[1]["operator"] = "<i>x</i>"
    section = one_set(tmp_path, data)

    assert "<b>Z</b>" not in section and "<i>x</i>" not in section
    assert "&lt;b&gt;Z&lt;/b&gt;" in section and "altered by &lt;i&gt;x&lt;/i&gt;" in section


def test_the_table_says_what_the_file_does_not_record(tmp_path):
    # Breaks if the table reads as if a rule's row included the runs that errored: the results
    # file holds counted runs only per rule, so the page must say that and show no per-rule
    # errored figure.
    section = one_set(tmp_path, appendix_data())

    assert "only the runs that were counted" in text_of(section)
    assert not any("errored" in label for label, _ in rows(section))
