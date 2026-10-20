"""The results page says what is true about the repair round and about extraction recall, both
read from files given on the command line (`--repair DIR`, `--recall FILE...`), never typed into
the code. Fixture numbers here are made up and chosen so no two of them coincide."""

import json
import re

import pytest

from report import panels, site
from report.tests.test_site import FIXTURES, load, text_of

TWO_ROUNDS = FIXTURES / "repair_out_two_rounds"
DEV = FIXTURES / "recall_dev.json"
REFERENCE = FIXTURES / "recall_reference.json"
CAVEAT = "Not a clean held-out measure"


def build(tmp_path, *extra: str, name: str = "results_rich") -> str:
    out = tmp_path / "index.html"
    argv = ["--results", str(FIXTURES / f"{name}.json"), "--out", str(out), *extra]
    assert site.main(argv) == 0
    return out.read_text(encoding="utf-8")


def strip_section(page: str) -> str:
    block = re.search(r'<section aria-labelledby="honesty-h".*?</section>', page, flags=re.S)
    assert block, "no honesty strip"
    return block.group(0)


def coverage_section(page: str) -> str:
    block = re.search(r'<section aria-labelledby="coverage-h".*?</section>', page, flags=re.S)
    assert block, "no coverage section"
    return block.group(0)


# --- the repair round ------------------------------------------------------------------------


def test_a_repair_folder_replaces_no_repair_run_yet_with_each_round_and_its_verdict(tmp_path):
    # Breaks if the page keeps saying "No repair run yet" when a repair folder holding two
    # summaries (a guard refusal, then a judged rejection) is given, or if either round, the
    # verdict or the judged numbers is missing.
    text = text_of(build(tmp_path, "--repair", str(TWO_ROUNDS)))

    assert "No repair run yet" not in text
    assert text.count("Rejected") >= 2
    assert "Violation rate 45% 44%" in text
    assert "Task success 90% 88%" in text
    assert "Runs 20 16" in text


def test_the_rejection_reason_of_a_refused_patch_is_on_the_page(tmp_path):
    # Breaks if a round the guard refused shows a verdict with no reason: the reader would not
    # learn that it never ran.
    text = text_of(build(tmp_path, "--repair", str(TWO_ROUNDS)))

    assert "guard: old_string must occur exactly once in app/prompts.py" in text
    assert "(2 tries)" in text
    assert "No diff: the patch was refused before it was applied." in text


def test_without_a_repair_folder_the_page_still_says_there_is_no_repair_run(tmp_path):
    # Breaks if the empty state is lost for a page built without the option.
    assert "No repair run yet" in text_of(build(tmp_path))


# --- extraction recall -----------------------------------------------------------------------


def test_without_the_options_the_page_is_the_bytes_the_golden_holds(tmp_path):
    # Breaks if adding the options changes the page of a run that does not use them.
    for name in ("results_rich", "results_empty"):
        page = build(tmp_path, name=name)
        assert page.encode() == (FIXTURES / f"golden_{name}.html").read_bytes()


def test_without_a_recall_file_the_page_says_not_measured_in_both_places(tmp_path):
    # Breaks if "not measured" disappears when nothing was measured, or only in one section.
    page = build(tmp_path)

    assert "not measured" in text_of(strip_section(page))
    assert "Extraction recall not measured" in text_of(coverage_section(page))
    assert CAVEAT not in page


def test_recall_files_show_each_set_with_its_numbers_and_the_caveat(tmp_path):
    # Breaks if a measured set is left out, if its counts or percentage are not the file's
    # (fixtures: 5 of 6 = 83%, 2 extra; 3 of 8 = 38%, 11 extra), if "not measured" stays, or if
    # the number appears without the warning that it is not held out.
    page = build(tmp_path, "--recall", str(DEV), str(REFERENCE))

    strip = text_of(strip_section(page))
    assert "5 of 6" in strip and "3 of 8" in strip
    assert "recall_dev" in strip and "recall_reference" in strip
    assert "83%" in strip and "38%" in strip
    assert "2 extra" in strip and "11 extra" in strip
    assert CAVEAT in strip
    cover = text_of(coverage_section(page))
    assert "recall_dev 5 of 6 (83%)" in cover and "recall_reference 3 of 8 (38%)" in cover
    assert CAVEAT in cover
    assert "not measured" not in text_of(page)


def test_the_sets_come_in_the_order_given(tmp_path):
    # Breaks if the order is sorted or changed: the reader would see a different first set.
    strip = text_of(strip_section(build(tmp_path, "--recall", str(REFERENCE), str(DEV))))

    assert strip.index("recall_reference") < strip.index("recall_dev")


def test_a_recall_in_the_results_file_is_kept_and_the_files_do_not_replace_it(tmp_path):
    # Breaks if the new option overrides a recall the results file already carries.
    data = load("results_rich.json")
    data["honesty"]["extraction_recall"] = 0.5
    results = tmp_path / "results.json"
    results.write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / "index.html"

    assert site.main(["--results", str(results), "--out", str(out), "--recall", str(DEV)]) == 0

    strip = text_of(strip_section(out.read_text(encoding="utf-8")))
    assert "50%" in strip and "5 of 6" not in strip and CAVEAT not in strip


def test_the_page_with_recall_files_has_no_script_and_no_external_url(tmp_path):
    # Breaks if the new markup brings a script or a link out of the page.
    page = build(tmp_path, "--recall", str(DEV), str(REFERENCE), "--repair", str(TWO_ROUNDS))

    assert "<script" not in page
    assert "http" not in page
    assert page == build(
        tmp_path, "--recall", str(DEV), str(REFERENCE), "--repair", str(TWO_ROUNDS)
    )


def test_a_set_name_is_escaped():
    # Breaks if a file name (the set's label) reaches the page as markup.
    view = panels.RecallView(name="<b>x</b>", matched=1, total=2, extra=0)

    page = panels.render_recall_items([view])

    assert "<b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[1, 2]",
        '{"matched": 1, "total": 2}',
        '{"matched": 3, "total": 2, "extra": 0}',
        '{"matched": 1, "total": 0, "extra": 0}',
        '{"matched": true, "total": 2, "extra": 0}',
        '{"matched": 1.5, "total": 2, "extra": 0}',
        '{"matched": -1, "total": 2, "extra": 0}',
        '{"matched": 1, "total": 2, "extra": -1}',
    ],
)
def test_a_recall_file_that_is_not_a_recall_measurement_exits_2(tmp_path, capsys, content):
    # Breaks if a malformed or impossible file (more hits than tries, a boolean, a fraction)
    # renders as a recall, or crashes with a traceback instead of the exit code the other
    # options use.
    bad = tmp_path / "bad.json"
    bad.write_text(content, encoding="utf-8")
    out = tmp_path / "x.html"

    code = site.main(["--results", str(FIXTURES / "results_rich.json"), "--out", str(out),
                      "--recall", str(bad)])  # fmt: skip

    assert code == 2
    assert "report.site:" in capsys.readouterr().err
    assert not out.exists()


def test_a_missing_recall_file_exits_2(tmp_path, capsys):
    # Breaks if a typo in --recall silently renders "not measured".
    out = tmp_path / "x.html"

    code = site.main(["--results", str(FIXTURES / "results_rich.json"), "--out", str(out),
                      "--recall", str(tmp_path / "nope.json")])  # fmt: skip

    assert code == 2
    assert not out.exists()
