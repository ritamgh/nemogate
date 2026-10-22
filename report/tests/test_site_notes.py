"""report/site.py --notes FILE: a section of free-text notes on the results page. The text lives
in a file given on the command line, never in the code. Fixture text here is made up."""

import json
import re

import pytest

from report import site
from report.tests.test_site import FIXTURES, load, text_of
from report.tests.test_site_appendix import appendix_data

NOTES = "First note, one line.\n\nSecond note,\nover two lines.\n\n\n\nThird note.\n"


def rich() -> dict:
    data = load("results_rich.json")
    data["constraints"]["C-002"]["has_check"] = True
    return data


def notes_section(page: str) -> str:
    block = re.search(r'<section aria-labelledby="notes-h".*?</section>', page, flags=re.S)
    assert block, "no notes section"
    return block.group(0)


def build(tmp_path, notes_text: str | None, name: str = "results_rich") -> bytes:
    out = tmp_path / "index.html"
    argv = ["--results", str(FIXTURES / f"{name}.json"), "--out", str(out)]
    if notes_text is not None:
        notes = tmp_path / "notes.txt"
        notes.write_text(notes_text, encoding="utf-8")
        argv += ["--notes", str(notes)]
    assert site.main(argv) == 0
    return out.read_bytes()


def test_without_the_option_the_page_is_the_bytes_the_golden_holds(tmp_path):
    # Breaks if adding the option changes the page of a run that does not use it.
    for name in ("results_rich", "results_empty"):
        assert build(tmp_path, None, name) == (FIXTURES / f"golden_{name}.html").read_bytes()


def test_an_empty_or_blank_file_adds_nothing(tmp_path):
    # Breaks if an empty notes file leaves a heading with no text under it.
    for blank in ("", "\n", " \n\n \t\n"):
        assert build(tmp_path, blank) == (FIXTURES / "golden_results_rich.html").read_bytes()


def test_paragraphs_come_in_file_order_under_the_heading(tmp_path):
    # Breaks if blank lines do not split paragraphs, if the order changes, or if a run of blank
    # lines makes an empty paragraph.
    section = notes_section(build(tmp_path, NOTES).decode())

    assert '<h2 id="notes-h">Notes on these numbers</h2>' in section
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", section, flags=re.S)
    assert [" ".join(p.split()) for p in paragraphs] == [
        "First note, one line.",
        "Second note, over two lines.",
        "Third note.",
    ]


def test_the_notes_sit_right_after_the_outcome_section_and_before_the_key(tmp_path):
    # Breaks if the notes land below the key and method text, far from the numbers they qualify.
    page = build(tmp_path, NOTES).decode()

    assert page.index("<!--/outcomes-->") < page.index('id="notes-h"') < page.index('id="key-h"')
    assert page.index('id="notes-h"') < page.index('id="fixes-h"')
    assert page.index("<!--/outcomes-->") + len("<!--/outcomes-->") == page.index(
        '<section aria-labelledby="notes-h"'
    )


def test_notes_on_a_page_with_no_constraints_still_follow_the_results_part(tmp_path):
    # Breaks if the notes are dropped, or break the page, when nothing was pooled.
    page = build(tmp_path, NOTES, "results_empty").decode()

    assert "Third note." in text_of(notes_section(page))
    assert page.index('id="notes-h"') < page.index('id="key-h"')


def test_markup_in_the_notes_is_escaped_not_injected(tmp_path):
    # Breaks if the note text is put in the page raw: a <script> would run, a tag would render.
    section = notes_section(
        build(tmp_path, 'Look <script>alert(x)</script> & <b>bold</b> "q".').decode()
    )

    assert "<script" not in section
    assert "<b>" not in section
    assert "&lt;script&gt;alert(x)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in section


def test_a_missing_notes_file_exits_2_with_a_message_and_writes_nothing(tmp_path, capsys):
    # Breaks if a bad --notes path is a traceback, is ignored, or leaves a half-made page.
    out = tmp_path / "index.html"
    code = site.main(
        [
            "--results",
            str(FIXTURES / "results_rich.json"),
            "--out",
            str(out),
            "--notes",
            str(tmp_path / "nope.txt"),
        ]
    )

    assert code == 2
    assert "report.site:" in capsys.readouterr().err
    assert not out.exists()


def test_the_page_with_notes_has_no_script_and_no_external_url(tmp_path):
    # Breaks if the notes make the page load code or reach the network.
    for name in ("results_rich", "results_empty"):
        page = build(tmp_path, NOTES + "\nMarkup <script>x()</script> too.\n", name).decode()

        assert "<script" not in page.lower()
        assert not re.search(r"(?:https?:)?//[a-z0-9]", page, flags=re.I)


def test_the_same_notes_render_the_same_bytes(tmp_path):
    # Breaks if the notes bring a clock or an unordered set into the page.
    assert build(tmp_path, NOTES) == build(tmp_path, NOTES)


SECRET = "withheld wording zzqx"
# Numbers of the rich fixture's page, checked by hand against its visible text: "120" is in the
# main set only, "9999" is only in the appendix built by `appendix_data()`, "20" and "95" are in
# the main set ("20" also in the appendix), and "424242" is in neither. The page shows no decimal.
MAIN_ONLY, APPENDIX_ONLY, BOTH, NOWHERE = "120", "9999", "20", "424242"


def run_main(tmp_path, capsys, main_notes: str | None = None, appendix_notes=None):
    """Build once. `main_notes` is the --notes text, `appendix_notes` a list with one notes text
    (or None) per appendix; returns (exit code, stderr lines, out path)."""
    out = tmp_path / "index.html"
    argv = ["--results", str(FIXTURES / "results_rich.json"), "--out", str(out)]
    if main_notes is not None:
        (tmp_path / "main-notes.txt").write_text(main_notes, encoding="utf-8")
        argv += ["--notes", str(tmp_path / "main-notes.txt")]
    for i, notes in enumerate(appendix_notes or [], 1):
        results = tmp_path / f"app{i}.json"
        results.write_text(json.dumps(appendix_data()), encoding="utf-8")
        argv += ["--appendix", f"Set {i}", str(results)]
        if notes is not None:
            (tmp_path / f"app{i}-notes.txt").write_text(notes, encoding="utf-8")
            argv.append(str(tmp_path / f"app{i}-notes.txt"))
    code = site.main(argv)
    return code, capsys.readouterr().err.splitlines(), out


def test_a_number_that_is_in_the_tables_builds(tmp_path, capsys):
    # Breaks if a notes file may not repeat a figure the page shows (the cross-check rejects
    # every digit instead of the ones the results file does not hold).
    code, err, out = run_main(tmp_path, capsys, f"The figure {MAIN_ONLY} is shown, as is {BOTH}.\n")

    assert (code, err) == (0, [])
    assert f"The figure {MAIN_ONLY} is shown, as is {BOTH}." in notes_section(out.read_text())


def test_a_number_in_no_table_fails_naming_file_line_and_number(tmp_path, capsys):
    # Breaks if a hand-typed number reaches the page, or if the message omits the file, the line
    # or the number, or spreads over several lines.
    text = f"Words only.\n\nThe second line has {MAIN_ONLY} and {NOWHERE}.\n"
    code, err, out = run_main(tmp_path, capsys, text)

    assert code == 2
    assert err == [
        f"report.site: {tmp_path / 'main-notes.txt'}: numbers the results do not hold "
        f"(line 3: {NOWHERE}); a note may only repeat a number the page shows"
    ]
    assert not out.exists()


def test_every_unmatched_number_of_a_file_is_reported_at_once(tmp_path, capsys):
    # Breaks if the build stops at the first bad number or line, so the author fixes one at a
    # time; also if a repeated number is listed twice or a matching one is listed at all.
    text = f"One {NOWHERE} and {NOWHERE} and 1.35.\nTwo {BOTH}.\n\nThree 31337 and {BOTH}.\n"
    _, err, _ = run_main(tmp_path, capsys, text)

    assert len(err) == 1
    assert f"(line 1: {NOWHERE}, 1.35; line 4: 31337)" in err[0]


def test_the_notes_text_cannot_satisfy_its_own_numbers(tmp_path, capsys):
    # Breaks if the reference text is taken from a page that already holds the notes, so a number
    # typed twice (or once) vouches for itself.
    code, err, _ = run_main(tmp_path, capsys, f"{NOWHERE} here.\n\n{NOWHERE} again.\n")

    assert code == 2
    assert f"line 1: {NOWHERE}; line 3: {NOWHERE}" in err[0]


def test_a_number_is_matched_whole_and_a_decimal_as_one_number(tmp_path, capsys):
    # Breaks if matching is by substring (999 inside 9999), if a decimal point splits a number
    # (95 and 5 are both on the page, 95.5 is not), or if a sentence's full stop is read as one.
    code, err, _ = run_main(tmp_path, capsys, "Not 95.5 here.\n")
    assert code == 2 and "(line 1: 95.5)" in err[0]

    code, err, _ = run_main(tmp_path, capsys, None, ["Not 999.\n"])
    assert code == 2 and "(line 1: 999)" in err[0]

    code, err, _ = run_main(tmp_path, capsys, "Up to 95. Then 95.\nAlso 120.\n")
    assert (code, err) == (0, [])


def test_the_numbers_are_those_of_the_set_the_notes_belong_to(tmp_path, capsys):
    # Breaks if every set is checked against one pool: the main notes would pass on a number only
    # an appendix shows, and an appendix's on a number only the main page shows.
    code, err, _ = run_main(tmp_path, capsys, f"{APPENDIX_ONLY} here.\n")
    assert code == 2 and f"line 1: {APPENDIX_ONLY}" in err[0]

    code, err, _ = run_main(tmp_path, capsys, None, [f"{MAIN_ONLY} here.\n"])
    assert code == 2 and f"line 1: {MAIN_ONLY}" in err[0]

    code, err, _ = run_main(tmp_path, capsys, f"{MAIN_ONLY}\n", [f"{APPENDIX_ONLY} {BOTH}\n"])
    assert (code, err) == (0, [])


def test_the_second_appendix_is_checked_against_its_own_section(tmp_path, capsys):
    # Breaks if every appendix is checked against the first (or all appendices pooled).
    code, err, out = run_main(tmp_path, capsys, None, [None, f"{APPENDIX_ONLY}\n"])
    assert (code, err) == (0, [])

    code, err, _ = run_main(tmp_path, capsys, None, [f"{BOTH}\n", f"{MAIN_ONLY}\n"])
    assert code == 2 and len(err) == 1
    assert "app2-notes.txt" in err[0] and f"line 1: {MAIN_ONLY}" in err[0]


def test_every_failing_file_gets_its_own_line(tmp_path, capsys):
    # Breaks if only the first failing file is reported, or an appendix's notes are not checked.
    code, err, out = run_main(tmp_path, capsys, f"{NOWHERE}\n", [f"{NOWHERE}\n"])

    assert code == 2
    assert len(err) == 2
    assert "main-notes.txt" in err[0] and "app1-notes.txt" in err[1]
    assert not out.exists()


def test_the_error_never_contains_the_text_of_the_line(tmp_path, capsys):
    # Breaks if the error echoes the offending line: appendix notes can sit next to withheld rule
    # text, and the rest of the line would be published to a terminal or a log.
    line = f"{SECRET} says {NOWHERE} more words\n"
    for main_notes, appendix in ((line, None), (None, [line])):
        code, err, _ = run_main(tmp_path, capsys, main_notes, appendix)

        assert code == 2
        joined = " ".join(err)
        assert NOWHERE in joined
        assert "zzqx" not in joined and "withheld" not in joined and "more words" not in joined


@pytest.mark.parametrize("where", ["main", "appendix"])
def test_line_numbers_survive_blank_lines_and_crlf(tmp_path, capsys, where):
    # Breaks if line numbers drift on blank lines or Windows line ends, so the author looks at
    # the wrong line.
    text = f"One.\r\n\r\nTwo.\r\n\r\nThree has {NOWHERE}.\r\nFour has {BOTH}.\r\n"
    notes = (text, None) if where == "main" else (None, [text])
    code, err, _ = run_main(tmp_path, capsys, *notes)

    assert code == 2
    assert f"(line 5: {NOWHERE})" in err[0]


def test_digits_of_other_scripts_are_numbers_compared_as_typed(tmp_path, capsys):
    # Breaks if Arabic-Indic or full-width digits slip through (a number the page does not show
    # as typed), or if they are dropped from the report.
    code, err, _ = run_main(tmp_path, capsys, "a number ٥ and ５ and 5 here\n")

    assert code == 2
    assert "(line 1: ٥, ５)" in err[0]


def test_notes_without_a_number_build_the_same_bytes_as_before(tmp_path, capsys):
    # Breaks if the check changes the page of a notes file that has no number, or if an empty
    # file, or no notes at all, no longer match the golden.
    words = "The third rule, one of two, was kept; roughly half. See the next paragraph.\n"
    code, err, out = run_main(tmp_path, capsys, words)
    page = out.read_bytes()

    assert (code, err) == (0, [])
    assert "The third rule, one of two, was kept; roughly half." in notes_section(page.decode())
    assert build(tmp_path, words) == page
    assert build(tmp_path, "") == (FIXTURES / "golden_results_rich.html").read_bytes()
    assert build(tmp_path, None) == (FIXTURES / "golden_results_rich.html").read_bytes()
