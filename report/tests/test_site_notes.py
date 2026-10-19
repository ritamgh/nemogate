"""report/site.py --notes FILE: a section of free-text notes on the results page. The text lives
in a file given on the command line, never in the code. Fixture text here is made up."""

import re

from report import site
from report.tests.test_site import FIXTURES, load, text_of

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
        build(tmp_path, 'Look <script>alert(1)</script> & <b>bold</b> "q".').decode()
    )

    assert "<script" not in section
    assert "<b>" not in section
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in section


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
