"""report/site.py --appendix TITLE RESULTS [NOTES]: a separate result set shown after the main
page, never pooled into it, with the rule wording withheld. Fixture text here is made up."""

import json
import re

import pytest

from report import site
from report.tests.test_site import FIXTURES, load, text_of

MARKER = "ZZ-UNIQUE-RULE-WORDING-7f3a"
WITHHELD = "Rule text withheld (held-out task)"
NOTES = "First appendix note.\n\nSecond note,\nover two lines.\n"
APPENDIX_REPO = "fedcba9876543210fedcba9876543210fedcba98"
APPENDIX_LEDGER = "99aa88bb77cc"


def appendix_data(marker: str = MARKER) -> dict:
    """results_rich.json made into a different run set: other repo commit and ledger, 9999 runs
    found, four mutation no-ops, and every rule's wording replaced by one marker string."""
    data = load("results_rich.json")
    for body in data["constraints"].values():
        body["text"] = marker
    data["reproducibility"]["repo_sha"] = APPENDIX_REPO
    data["reproducibility"]["ledger_hash"] = APPENDIX_LEDGER
    data["runs"]["found"] = 9999
    data["runs"]["inconclusive"]["mutation_noop"] = 4
    return data


def write_json(path, data) -> str:
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def build(tmp_path, appendices=(), results: str = "results_rich.json", name: str = "index.html"):
    """Render the page. `appendices`: (title, results dict, notes text or None) each."""
    out = tmp_path / name
    argv = ["--results", str(FIXTURES / results), "--out", str(out)]
    for i, (title, data, notes) in enumerate(appendices):
        argv += ["--appendix", title, write_json(tmp_path / f"app{i}.json", data)]
        if notes is not None:
            note_file = tmp_path / f"app{i}-notes.txt"
            note_file.write_text(notes, encoding="utf-8")
            argv.append(str(note_file))
    assert site.main(argv) == 0
    return out.read_bytes().decode()


def main_part(page: str) -> str:
    """Everything the main page is made of: from <body> up to the appendices (or the end)."""
    end = page.index("<!--appendices-->") if "<!--appendices-->" in page else page.index("</main>")
    return page[page.index("<body>") : end]


def appendix_part(page: str) -> str:
    return page[page.index("<!--appendices-->") : page.index("</main>")]


def test_without_the_option_the_page_is_the_bytes_the_golden_holds(tmp_path):
    # Breaks if adding the option changes the page of a run that does not use it.
    for name in ("results_rich", "results_empty"):
        page = build(tmp_path, results=f"{name}.json")
        assert page.encode() == (FIXTURES / f"golden_{name}.html").read_bytes()


def test_an_appendix_renders_its_title_tables_and_accounting(tmp_path):
    # Breaks if the appendix is not drawn from its own file: no heading, no outcome tables, no
    # intervals, or the accounting is missing or is the main set's.
    page = build(tmp_path, [("Second set, first run", appendix_data(), None)])
    part = appendix_part(page)
    text = text_of(part)

    assert '<h2 id="appendix-1-h">Second set, first run</h2>' in part
    assert "What happened in the runs of C-001" in part
    assert 'class="outcome"' in part
    assert "Did the job without breaking the rule" in text
    assert "Counts are small; read the intervals" in text
    assert "95% Wilson interval" in part  # the intervals are visible, as in the main tables
    assert re.search(r"found\s*9999", text)  # 9999 is the appendix file's count, not the main's
    assert "not comparable 9" in text
    assert "model/sut-y" in text
    assert '<code title="fedcba9876543210fedcba9876543210fedcba98">fedcba987654</code>' in part


def test_the_main_part_of_the_page_is_unchanged_by_an_appendix(tmp_path):
    # Breaks if any appendix number leaks into the verdict, key, graph, header or main tables.
    without = build(tmp_path, name="without.html")
    with_app = build(
        tmp_path,
        [("Set one", appendix_data(), NOTES), ("Set two", appendix_data(), None)],
        name="with.html",
    )

    assert main_part(with_app) == without[without.index("<body>") : without.index("</main>")]
    assert "9999" not in main_part(with_app)
    assert "9999" in appendix_part(with_app)


def test_the_appendices_sit_after_everything_on_the_main_page(tmp_path):
    # Breaks if an appendix lands between main sections, where it reads as part of the result.
    page = build(tmp_path, [("Set one", appendix_data(), None)])

    assert page.index("<footer>") < page.index('id="appendix-1-h"') < page.index("</main>")


def test_an_appendix_on_a_page_with_no_constraints_still_renders(tmp_path):
    # Breaks if the appendix assumes the main set has rules or a recorded set of conditions.
    page = build(tmp_path, [("Set one", appendix_data(), None)], results="results_empty.json")

    assert "Set one" in appendix_part(page)
    assert main_part(page) == main_part(build(tmp_path, results="results_empty.json"))


def test_rule_wording_of_an_appendix_never_reaches_the_page(tmp_path):
    # Breaks if an appendix card (heading, table label or tooltip) prints a rule's text. The same
    # file given as --results must still show it, so the check is not vacuous.
    data = appendix_data()
    page = build(tmp_path, [("Set one", data, None)])
    as_main = tmp_path / "as_main.html"
    assert (
        site.main(
            ["--results", write_json(tmp_path / "m.json", data), "--out", str(as_main)],
        )
        == 0
    )

    assert MARKER not in page
    assert MARKER in as_main.read_text(encoding="utf-8")
    assert page.count(WITHHELD) == len(data["constraints"])


def test_an_appendix_card_shows_only_the_rule_id_kind_and_counts(tmp_path):
    # Breaks if withholding removes the id or kind along with the wording, or the task ids and
    # other free text of the rule come back.
    data = appendix_data()
    page = build(tmp_path, [("Set one", data, None)])
    part = appendix_part(page)

    assert "<code>C-001</code>" in part
    assert f'<span class="badge">{data["constraints"]["C-001"]["kind"]}</span>' in part
    assert "Tasks that contributed" not in part
    assert "task TA" not in text_of(part)


def test_two_appendices_from_the_same_rules_do_not_share_an_id(tmp_path):
    # Breaks if both appendices use the same ids for the same rule ids: duplicate anchors, and a
    # link that lands in the wrong set.
    page = build(tmp_path, [("Set one", appendix_data(), None), ("Set two", appendix_data(), None)])
    ids = re.findall(r'\bid="([^"]+)"', page)

    assert len(ids) == len(set(ids))
    assert 'id="appendix-1-h"' in page
    assert 'id="appendix-2-h"' in page
    for href in re.findall(r'href="#([^"]+)"', page):
        assert href in ids


def test_appendix_notes_come_in_file_order_and_are_escaped(tmp_path):
    # Breaks if the notes are not split on blank lines, or if markup in them is put in raw.
    page = build(
        tmp_path,
        [("Set one", appendix_data(), NOTES + "\nLook <script>alert(1)</script> & <b>x</b>.\n")],
    )
    part = appendix_part(page)

    assert "<script" not in part.lower()
    assert "<b>x" not in part
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;x&lt;/b&gt;." in part
    text = text_of(part)
    assert text.index("First appendix note.") < text.index("Second note, over two lines.")


def test_the_title_is_escaped(tmp_path):
    # Breaks if the title, which comes from the command line, is put in the page raw.
    page = build(tmp_path, [('A <i>"b"</i> & c', appendix_data(), None)])

    assert "<i>" not in page
    assert "A &lt;i&gt;&quot;b&quot;&lt;/i&gt; &amp; c" in appendix_part(page)


def test_it_says_how_the_runs_differ_using_only_the_two_files(tmp_path):
    # Breaks if the "how these runs differ" lines are invented, miss a changed commit or ledger,
    # or hide the mutation no-op count.
    page = build(tmp_path, [("Set one", appendix_data(), None)])
    text = text_of(appendix_part(page))

    assert "How these runs differ from the main set" in text
    assert "repo commit fedcba987654 here, 0123456789ab in the main set" in text
    assert "ledger 99aa88bb77cc here, 1f2e3d4c5b6a in the main set" in text
    assert "mutation no-ops: 4 here, none recorded in the main set" in text
    differs = re.search(r"<h3[^>]*>How these runs differ.*?</ul>", appendix_part(page), flags=re.S)
    assert differs and "lockfile" not in differs.group(0)  # the lockfile is the same in both


def test_identical_conditions_are_said_to_be_identical(tmp_path):
    # Breaks if the section claims a difference that is not in the data.
    same = load("results_rich.json")
    part = appendix_part(build(tmp_path, [("Set one", same, None)]))

    assert "Same conditions as the main set." in text_of(part)
    assert " here, " not in text_of(part)


@pytest.mark.parametrize("what", ["missing", "bad_json", "wrong_version", "not_an_object"])
def test_a_bad_appendix_results_file_exits_2_and_writes_nothing(tmp_path, capsys, what):
    # Breaks if a bad appendix file is a traceback, is skipped, or leaves a half-made page.
    path = tmp_path / "app.json"
    if what == "bad_json":
        path.write_text("{nope", encoding="utf-8")
    elif what == "wrong_version":
        write_json(path, {**appendix_data(), "format_version": 1})
    elif what == "not_an_object":
        write_json(path, [1, 2])
    out = tmp_path / "index.html"
    code = site.main(
        [
            "--results",
            str(FIXTURES / "results_rich.json"),
            "--out",
            str(out),
            "--appendix",
            "Set one",
            str(path),
        ]
    )

    assert code == 2
    assert "report.site:" in capsys.readouterr().err
    assert not out.exists()


def test_a_missing_appendix_notes_file_exits_2_and_writes_nothing(tmp_path, capsys):
    # Breaks if a bad notes path is ignored and the page is written without them.
    out = tmp_path / "index.html"
    code = site.main(
        [
            "--results",
            str(FIXTURES / "results_rich.json"),
            "--out",
            str(out),
            "--appendix",
            "Set one",
            write_json(tmp_path / "app.json", appendix_data()),
            str(tmp_path / "nope.txt"),
        ]
    )

    assert code == 2
    assert "report.site:" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("values", [["only a title"], ["a", "b", "c", "d"]])
def test_the_wrong_number_of_values_is_an_argument_error(tmp_path, values):
    # Breaks if --appendix with too few or too many values is accepted or crashes.
    with pytest.raises(SystemExit) as exc:
        site.main(
            [
                "--results",
                str(FIXTURES / "results_rich.json"),
                "--out",
                str(tmp_path / "index.html"),
                "--appendix",
                *values,
            ]
        )

    assert exc.value.code == 2


def test_the_page_with_appendices_has_no_script_and_no_external_url(tmp_path):
    # Breaks if an appendix makes the page load code or reach the network.
    page = build(
        tmp_path, [("Set one", appendix_data(), NOTES), ("Set two", appendix_data(), None)]
    )

    assert "<script" not in page.lower()
    assert not re.search(r"(?:https?:)?//[a-z0-9]", page, flags=re.I)


def test_the_same_inputs_render_the_same_bytes(tmp_path):
    # Breaks if an appendix brings a clock, a path or an unordered set into the page.
    args = [("Set one", appendix_data(), NOTES), ("Set two", appendix_data(), None)]

    assert build(tmp_path, args, name="a.html") == build(tmp_path, args, name="b.html")
