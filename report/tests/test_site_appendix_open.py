"""report/site.py --appendix-open TITLE RESULTS [NOTES]: a separately labelled result set for a task
that is not sealed, so its rule text and task ids are shown; and the errored runs per arm that
every appendix says. Fixture text here is made up."""

import re

import pytest

from report import site
from report.tests.test_site import FIXTURES, text_of
from report.tests.test_site_accounting import rows, sections
from report.tests.test_site_appendix import (
    MARKER,
    WITHHELD,
    appendix_data,
    appendix_part,
    main_part,
    write_json,
)
from report.tests.test_site_cost import page as cost_fixture


def cost_page(text: str) -> dict:
    """One graded rule with exact tests and cell costs, so "At a glance" and "Cost of prevention"
    both render (the fixture of test_site_cost)."""
    return cost_fixture(text=text)


ERRORED = {
    "count": 21,
    "cost_usd": 0.0573,
    "cost_unknown": 0,
    "by_arm": {
        "block_only": {"count": 8, "cost_usd": 0.0386, "cost_unknown": 0},
        "gated": {"count": 7, "cost_usd": 0.007, "cost_unknown": 0},
        "mutated": {"count": 6, "cost_usd": 0.0117, "cost_unknown": 0},
    },
}


def with_errors(data: dict) -> dict:
    data["runs"]["errored_cost"] = ERRORED
    return data


def build(tmp_path, appendices=(), name: str = "index.html") -> str:
    """Render the page. `appendices`: (title, results dict, notes text or None, open?) each, in
    command-line order."""
    out = tmp_path / name
    argv = ["--results", str(FIXTURES / "results_rich.json"), "--out", str(out)]
    for i, (title, data, notes, is_open) in enumerate(appendices):
        argv += [
            "--appendix-open" if is_open else "--appendix",
            title,
            write_json(tmp_path / f"app{i}.json", data),
        ]
        if notes is not None:
            note_file = tmp_path / f"app{i}-notes.txt"
            note_file.write_text(notes, encoding="utf-8")
            argv.append(str(note_file))
    assert site.main(argv) == 0
    return out.read_bytes().decode()


def test_an_open_appendix_shows_the_rule_text_and_the_task_ids(tmp_path):
    # Breaks if --appendix-open still withholds the wording, or drops the task ids that tell the
    # reader which task the set is about.
    page = build(tmp_path, [("Research set", appendix_data(), None, True)])
    part = appendix_part(page)
    text = text_of(part)

    assert MARKER in text
    assert WITHHELD not in text
    assert "task TA" in text and "tasks TA, TB" in text
    assert "Not a sealed task" in text
    assert "Counts are small; read the intervals" in text  # the small-counts warning stays


def test_the_rule_text_of_an_open_appendix_is_in_its_cost_table_too(tmp_path):
    # Breaks if the "Cost of prevention" row says "withheld" (or the wording) for the wrong kind of
    # set: an open set shows the rule, a sealed one never does.
    wording = "COST-ROW-WORDING"
    data = cost_page(wording)
    out = tmp_path / "index.html"
    argv = ["--results", str(FIXTURES / "results_rich.json"), "--out", str(out)]

    for option, shown in (("--appendix-open", True), ("--appendix", False)):
        assert site.main([*argv, option, "Set", write_json(tmp_path / "a.json", data)]) == 0
        page = out.read_text(encoding="utf-8")
        cost = re.search(r'<section class="ogroup cost".*?</section>', page, flags=re.S)

        assert cost, "the fixture must produce a cost table"
        assert (wording in cost.group(0)) is shown
        assert (WITHHELD in cost.group(0)) is not shown


def test_the_rule_text_of_an_open_appendix_is_in_its_glance_and_cost_tables_too(tmp_path):
    # Breaks if only the card shows the wording and the "At a glance" row says "withheld".
    data = appendix_data()
    for body in data["constraints"].values():
        for b in body["boundaries"].values():
            b["exact_tests"] = [
                {
                    "name": "violations",
                    "operator": "drop",
                    "compaction": {"keep": None, "tokens": None},
                    "a": {"arm": "gated", "operator": "drop"},
                    "b": {"arm": "block_only", "operator": "drop"},
                    "test": "mcnemar",
                    "k_a": 0,
                    "k_b": 3,
                    "n_a": 8,
                    "n_b": 8,
                    "n_pairs": 8,
                    "n_sources": 8,
                    "n_unmatched": 0,
                    "discordant": {"a_only": 0, "b_only": 3},
                    "by_source": {"a_only": 0, "b_only": 3, "ties": 5},
                    "p_exact": 0.25,
                    "p_exact_by_source": 0.25,
                }
            ]
    page = build(tmp_path, [("Research set", data, None, True)])
    glance = re.search(r'<section class="ogroup glance".*?</section>', page, flags=re.S)

    assert glance and MARKER in glance.group(0)
    assert WITHHELD not in glance.group(0)


def test_a_sealed_appendix_next_to_an_open_one_stays_withheld(tmp_path):
    # Breaks if the open flag leaks to the other appendix, or the numbering is not the order the
    # options were given: appendix 1 sealed, 2 open, 3 sealed.
    page = build(
        tmp_path,
        [
            ("Sealed one", appendix_data("SEALED-ONE-WORDING"), None, False),
            ("Open two", appendix_data("OPEN-TWO-WORDING"), None, True),
            ("Sealed three", appendix_data("SEALED-THREE-WORDING"), None, False),
        ],
    )

    assert "SEALED-ONE-WORDING" not in page and "SEALED-THREE-WORDING" not in page
    assert "OPEN-TWO-WORDING" in page
    two = re.split(r'(?=<section class="appendix" id="appendix-)', appendix_part(page))[2]
    assert 'id="appendix-2-h">Open two<' in two
    assert "OPEN-TWO-WORDING" in two and WITHHELD not in two


def test_the_main_part_is_unchanged_by_an_open_appendix(tmp_path):
    # Breaks if open text, the errored table or any appendix number leaks into the main page.
    without = build(tmp_path, name="without.html")
    with_open = build(
        tmp_path, [("Research set", with_errors(appendix_data("OPEN-WORDING")), None, True)]
    )

    assert main_part(with_open) == main_part(without)
    assert "OPEN-WORDING" not in main_part(with_open)


def test_the_text_of_an_open_appendix_is_escaped(tmp_path):
    # Breaks if shown rule text goes into the page raw: results_rich.json holds markup in C-002.
    from report.tests.test_site import load

    data = load("results_rich.json")
    page = build(tmp_path, [("Research set", data, None, True)])

    assert "<script>alert" not in appendix_part(page)
    assert "&lt;script&gt;alert(" in appendix_part(page)


def test_errored_runs_are_listed_per_arm_with_zero_for_an_arm_with_none(tmp_path):
    # Breaks if the table takes the totals from another place than errored_cost.by_arm, leaves
    # out an arm with no errored run (it must read 0: errored_cost lists only arms that had
    # one), or shows a dash for a figure the file does hold. Literals from ERRORED and from
    # results_rich.json (counted: natural 30, mutated 30, block-only 20, gated 20).
    section = sections(build(tmp_path, [("Set", with_errors(appendix_data()), None, True)]))[0]
    table = rows(section)[-5:]

    assert table == [
        ("Natural", ["0", "30"]),
        ("Mutated", ["6", "30"]),
        ("Block-only", ["8", "20"]),
        ("Gated (NemoGate)", ["7", "20"]),
        ("All arms", ["21", "100"]),
    ]


def test_the_errored_table_says_in_plain_words_what_an_errored_run_is(tmp_path):
    # Breaks if the page shows the counts without saying that an errored run is in no rate, so
    # the reader takes fewer counted runs for fewer violations.
    section = sections(build(tmp_path, [("Set", with_errors(appendix_data()), None, True)]))[0]
    text = text_of(section)

    assert "Errored runs, by arm" in text
    assert "ended in an error before it could be graded" in text
    assert "in no rate" in text


def test_sealed_appendices_get_the_errored_table_too(tmp_path):
    # Breaks if the table is only for open sets: errors per arm is a fact of any run set.
    section = sections(build(tmp_path, [("Set", with_errors(appendix_data()), None, False)]))[0]

    assert ("Gated (NemoGate)", ["7", "20"]) in rows(section)


def test_no_errored_table_when_the_file_has_no_errored_cost(tmp_path):
    # Breaks if a file from before errored_cost gets a table of invented zeros.
    section = sections(build(tmp_path, [("Set", appendix_data(), None, True)]))[0]

    assert "<h4>Errored runs" not in section


def test_the_accounting_note_does_not_deny_errors_per_arm(tmp_path):
    # Breaks if the note still says the file holds no record of an errored run for a single arm,
    # next to a table that lists exactly that.
    section = sections(build(tmp_path, [("Set", with_errors(appendix_data()), None, True)]))[0]

    assert "no record of a run that errored or was left out for a single rule, arm" not in section


def test_a_note_of_an_open_appendix_may_repeat_a_number_the_set_shows(tmp_path):
    # Breaks if the notes check reads the open set as sealed, or refuses a number that is on its
    # page (21 is the errored total in the table, 9999 the runs found).
    page = build(
        tmp_path,
        [
            (
                "Set",
                with_errors(appendix_data()),
                "21 of 9999 runs errored.\n",
                True,
            )
        ],
    )

    assert "21 of 9999 runs errored." in text_of(appendix_part(page))


def test_a_note_may_repeat_a_number_only_an_open_sets_rule_text_shows(tmp_path, capsys):
    # Breaks if the notes check drops the open flag and reads the set as sealed: the number 7777 is
    # only in the rule's wording, which a sealed page never prints, so the same files must be
    # accepted with --appendix-open and refused with --appendix.
    data = appendix_data("Never exceed 7777 requests")
    notes = tmp_path / "notes.txt"
    notes.write_text("The limit of 7777 matters here.\n", encoding="utf-8")
    argv = ["--results", str(FIXTURES / "results_rich.json"), "--out", str(tmp_path / "o.html")]
    files = [write_json(tmp_path / "app.json", data), str(notes)]

    assert site.main([*argv, "--appendix-open", "Set", *files]) == 0
    assert site.main([*argv, "--appendix", "Set", *files]) == 2
    assert "7777" in capsys.readouterr().err


def test_an_errored_run_with_no_recorded_arm_is_listed_not_dropped(tmp_path):
    # Breaks if the by-arm rows leave out an arm name the four known ones do not cover, so the
    # rows no longer add up to the "All arms" row (here 3 + 1 = 4).
    data = appendix_data()
    data["runs"]["errored_cost"] = {
        "count": 4,
        "cost_usd": 0.0,
        "cost_unknown": 4,
        "by_arm": {
            "gated": {"count": 3, "cost_usd": 0.0, "cost_unknown": 3},
            "unknown": {"count": 1, "cost_usd": 0.0, "cost_unknown": 1},
        },
    }
    section = sections(build(tmp_path, [("Set", data, None, True)]))[0]

    assert rows(section)[-3:] == [
        ("Gated (NemoGate)", ["3", "20"]),
        ("unknown", ["1", "—"]),
        ("All arms", ["4", "100"]),
    ]


def test_a_note_with_a_number_the_set_does_not_show_exits_2(tmp_path, capsys):
    # Breaks if the number check is skipped for open sets.
    out = tmp_path / "index.html"
    notes = tmp_path / "notes.txt"
    notes.write_text("It was 31337 runs.\n", encoding="utf-8")
    code = site.main(
        [
            "--results",
            str(FIXTURES / "results_rich.json"),
            "--out",
            str(out),
            "--appendix-open",
            "Set",
            write_json(tmp_path / "app.json", appendix_data()),
            str(notes),
        ]
    )

    assert code == 2
    assert "31337" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("values", [["only a title"], ["a", "b", "c", "d"]])
def test_the_wrong_number_of_values_for_the_open_option_is_an_argument_error(tmp_path, values):
    # Breaks if --appendix-open with too few or too many values is accepted or crashes.
    with pytest.raises(SystemExit) as exc:
        site.main(
            [
                "--results",
                str(FIXTURES / "results_rich.json"),
                "--out",
                str(tmp_path / "index.html"),
                "--appendix-open",
                *values,
            ]
        )

    assert exc.value.code == 2


def test_the_same_inputs_render_the_same_bytes_with_an_open_appendix(tmp_path):
    # Breaks if the open path brings a clock, a path or an unordered set into the page.
    args = [("Set", with_errors(appendix_data()), None, True)]

    assert build(tmp_path, args, name="a.html") == build(tmp_path, args, name="b.html")
