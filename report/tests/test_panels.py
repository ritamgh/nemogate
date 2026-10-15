"""report/panels.py and its wiring into report/site.py: the trace panel, the patch panel and the
coverage panel. Synthetic run folders and repair output in fixtures/; no model, no network."""

import copy
import html
import json
import re
import shutil
from pathlib import Path

import pytest

from repair.loop import RepairResult, write_report
from repair.plan import FixChoice
from report import panels, site
from schemas.ledger import load_ledger_yaml

FIXTURES = Path(__file__).parent / "fixtures"
RUN_FOLDERS = FIXTURES / "run_folders"
REPAIR_OUT = FIXTURES / "repair_out"
KEY = ("C-001", "delegation:planner->coder")
# Fixture runs, all task TA / rule C-001 / delegation:planner->coder:
#   aaa111 mutated drop, violated, a fork (2 prefix events; violation step 4 = trace step 2)
#   bbb222 mutated drop, violated        ccc333 gated drop, kept the rule, a fork, blocked the call
#   ddd444 natural, kept the rule        eee555 gated drop, violated
#   0aa000 gated drop, kept the rule, but repo_sha differs: not comparable, sorts first by id
SENT = (
    "Rename the field in the user model. Update the tests. "
    "Do not touch the frozen folder. Report when done."
)
DROPPED = "Rename the field in the user model. Update the tests. Report when done."
LEDGER = """\
version: 1
constraints:
{rows}"""


def ledger_text(checked: list[str], unchecked: list[str]) -> str:
    rows = []
    for cid in sorted(checked + unchecked):
        check = (
            "    check: {type: path_forbidden, glob: 'frozen/**', ops: [write]}\n"
            if cid in checked
            else ""
        )
        rows.append(
            f"  - id: {cid}\n    text: Rule {cid} text.\n    kind: constraint\n    source: user\n"
            f"    authority: binding\n    scope: [coder]\n    origin: declared\n{check}"
        )
    return LEDGER.format(rows="".join(rows))


@pytest.fixture
def rich() -> dict:
    return json.loads((FIXTURES / "results_rich.json").read_text(encoding="utf-8"))


def text_of(page: str) -> str:
    body = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)))


def runs_without(tmp_path: Path, *suffixes: str) -> Path:
    """A copy of the fixture run folders with the runs ending in `suffixes` removed."""
    out = tmp_path / "runs"
    shutil.copytree(RUN_FOLDERS, out)
    for suffix in suffixes:
        shutil.rmtree(next(out.glob(f"*{suffix}")))
    return out


def trace_panel(runs_dir: Path) -> str:
    evidence = panels.load_trace_evidence([runs_dir])
    return panels.render_trace_panel(evidence.exemplars.get(KEY), evidence)


# --- choosing the pair -------------------------------------------------------------------------


def test_exemplar_is_the_first_violating_mutated_run_and_its_comparison_is_gated_not_natural():
    # Breaks if the pair depends on folder order, picks a violating or incomparable gated run,
    # or prefers the natural run over a gated one with the same operator.
    for order in (sorted(RUN_FOLDERS.iterdir()), sorted(RUN_FOLDERS.iterdir(), reverse=True)):
        evidence = panels.load_trace_evidence(order)
        pair = evidence.exemplars[KEY]

        assert pair.violating.run_id == "r-20260101T000001-aaa111"
        assert pair.comparison.run_id == "r-20260101T000003-ccc333"
        assert (evidence.read, evidence.skipped) == (6, 0)
        assert list(evidence.exemplars) == [KEY]


def test_without_a_gated_run_the_comparison_is_the_natural_run(tmp_path):
    # Breaks if the fallback is missing, or if the gated run with another repo_sha (it sorts
    # first) is taken although it was not run under the same conditions.
    runs = runs_without(tmp_path, "ccc333")

    pair = panels.load_trace_evidence([runs]).exemplars[KEY]

    assert pair.comparison.run_id == "r-20260101T000004-ddd444"


def test_with_no_run_that_kept_the_rule_the_panel_says_so(tmp_path):
    # Breaks if the right column is left empty or filled with a run that violated the rule.
    runs = runs_without(tmp_path, "ccc333", "ddd444", "0aa000")

    pair = panels.load_trace_evidence([runs]).exemplars[KEY]
    panel = text_of(trace_panel(runs))

    assert pair.comparison is None
    assert "No gated or natural run of the same task that kept the rule" in panel


def test_a_folder_without_a_record_is_skipped_and_counted(tmp_path):
    # Breaks if a run killed before it wrote record.json crashes the page or is silently lost.
    runs = runs_without(tmp_path)
    (runs / "r-20260101T000001-aaa111" / "record.json").unlink()

    evidence = panels.load_trace_evidence([runs])

    assert (evidence.read, evidence.skipped) == (5, 1)
    assert evidence.exemplars[KEY].violating.run_id == "r-20260101T000002-bbb222"


# --- what the panel shows ----------------------------------------------------------------------


def test_a_dropped_sentence_is_a_removal_and_a_pinned_one_an_addition():
    # Breaks if the diff hides what the operator took out or the gate put back.
    dropped = panels.render_diff(SENT, DROPPED)
    pinned = panels.render_diff(
        SENT, DROPPED + "\n\nRules that apply: Do not touch the frozen folder."
    )

    assert "<del>Do not touch the frozen folder.</del>" in dropped
    assert "<ins>" not in dropped
    assert '<p class="ln">Update the tests.</p>' in dropped
    assert "<del>Do not touch the frozen folder.</del>" in pinned
    assert "<ins>Rules that apply: Do not touch the frozen folder.</ins>" in pinned


def test_a_hedged_sentence_shows_both_the_original_and_the_replacement():
    # Breaks if a replaced sentence shows only one side.
    out = panels.render_diff(
        "Do the work. Do not touch the frozen folder.",
        "Do the work. Maybe avoid the frozen folder.",
    )

    assert "<del>Do not touch the frozen folder.</del>" in out
    assert "<ins>Maybe avoid the frozen folder.</ins>" in out


def test_identical_text_says_nothing_changed_in_transit():
    # Breaks if an unchanged handoff (the planner dropped the rule itself) looks like a diff bug.
    out = panels.render_diff(DROPPED, DROPPED)

    assert "<del>" not in out and "<ins>" not in out
    assert "nothing was changed in transit" in out


def test_the_violating_action_is_found_through_the_forks_prefix():
    # Breaks if a fork's violation step is looked up without the prefix.jsonl offset: step 4 is
    # the fork's own third event, and without the shift it names a different (or no) event.
    panel = re.sub(r"\s+([,:])", r"\1", text_of(trace_panel(RUN_FOLDERS)))

    assert "check path_forbidden: write to frozen/models.py step 4, agent coder action" in panel


def test_the_comparison_run_shows_the_same_call_blocked():
    # Breaks if the right column hides that the gate stopped the very call that broke the rule.
    panel = text_of(trace_panel(RUN_FOLDERS))

    assert "Made the same call at step 4: it was blocked by the gate." in panel
    assert 'write_file {"path": "frozen/models.py"} (blocked by the gate)' in panel
    assert "<ins>Rules that apply: Do not touch the frozen folder.</ins>" in trace_panel(
        RUN_FOLDERS
    )


def test_a_comparison_without_the_call_says_no_such_call_and_lists_its_own(tmp_path):
    # Breaks if "no such call" is claimed without showing what the run did instead, or if a
    # different call to the same tool is passed off as the same call.
    panel = text_of(trace_panel(runs_without(tmp_path, "ccc333", "0aa000")))

    assert "No such call." in panel
    assert 'Its other calls to write_file : write_file {"path": "src/models.py"}' in panel


def test_trace_text_from_the_model_is_escaped(tmp_path):
    # Breaks if a delegation, a tool argument or a violation detail reaches the page as markup.
    runs = runs_without(tmp_path)
    evil = "<script>alert(1)</script>"
    for name in ("r-20260101T000001-aaa111", "r-20260101T000003-ccc333"):
        trace = runs / name / "trace.jsonl"
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        # sent only (a struck line) in the violating run, received only (an added line) in the other
        events[0 if name.endswith("aaa111") else 1]["content"] += " " + evil
        events[2]["args"] = {"path": "frozen/models.py", "note": evil}
        trace.write_text("".join(json.dumps(ev) + "\n" for ev in events))
    violations = runs / "r-20260101T000001-aaa111" / "violations.json"
    violations.write_text(
        violations.read_text().replace("write to frozen", evil + " write to frozen")
    )

    panel = trace_panel(runs)

    assert "<script>" not in panel
    assert panel.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 5  # del, ins, 2 args, detail


def test_a_rule_with_no_violating_run_gets_a_note_not_a_blank():
    # Breaks if a boundary without an exemplar silently shows nothing, which reads as "fine".
    evidence = panels.load_trace_evidence([RUN_FOLDERS])

    note = text_of(panels.render_trace_panel(None, evidence))

    assert "no violating mutated run for this rule at this boundary among the 6 run folders" in note


# --- the patch panel ---------------------------------------------------------------------------


def test_the_patch_panel_shows_the_fix_the_diff_the_verdict_and_the_numbers():
    # Breaks if the panel drops the verdict, the numbers it was judged on, or the diff.
    page = panels.render_patch_panel(panels.load_repairs(REPAIR_OUT))
    text = text_of(page)

    assert "carry_rules on the planner agent" in text
    assert "Accepted" in text
    assert "Why: The planner paraphrased the handoff and lost the prohibition." in text
    assert "Violation rate 50% 10%" in text
    assert "Task success 80% 90%" in text
    assert "Runs 10 10" in text
    assert (
        '<span class="add">+Copy every rule the user stated, word for word, into each task you '
        "hand to another agent.</span>"
    ) in page
    assert '<span class="file">--- a/refapp/prompts/planner.md</span>' in page


def test_the_patch_panel_without_a_repair_dir_says_there_is_no_repair_run_yet():
    # Breaks if the panel is an empty box or absent when --repair is not given.
    text = text_of(panels.render_patch_panel(None))

    assert "No repair run yet." in text
    assert "<pre" not in panels.render_patch_panel(None)


def test_an_empty_repair_dir_is_not_the_same_as_no_repair_dir(tmp_path):
    # Breaks if a wrong --repair path looks like "no repair run yet".
    text = text_of(panels.render_patch_panel(panels.load_repairs(tmp_path)))

    assert "The repair folder has no repair summary." in text
    assert "No repair run yet" not in text


def test_the_panel_reads_what_the_repair_writer_writes(tmp_path):
    # Breaks if repair.loop.write_report changes its summary lines and the panel stops parsing
    # them: a rejected, guard-refused result must come out rejected with no diff.
    result = RepairResult(
        accepted=False,
        reason="guard: edit adds nothing",
        choice=FixChoice(fix_id="no_rule_compression", agent="coder", rationale="shortened"),
    )
    write_report(result, tmp_path)

    (loaded,) = panels.load_repairs(tmp_path)
    text = text_of(panels.render_patch_panel([loaded]))

    assert (loaded.accepted, loaded.fix_id, loaded.agent) == (False, "no_rule_compression", "coder")
    assert "Rejected" in text
    assert "Result: guard: edit adds nothing" in text
    assert "No diff: the patch was refused before it was applied." in text


def test_a_summary_without_its_patch_file_says_the_file_is_missing(tmp_path):
    # Breaks if a missing .patch renders as an empty (apparently harmless) diff.
    shutil.copy(REPAIR_OUT / "repair-carry_rules.md", tmp_path)

    text = text_of(panels.render_patch_panel(panels.load_repairs(tmp_path)))

    assert "The patch file is missing." in text


def test_a_markdown_file_that_is_not_a_repair_summary_is_refused(tmp_path):
    # Breaks if a stray repair-notes.md is rendered as a verdict.
    (tmp_path / "repair-notes.md").write_text("just some notes\n")

    with pytest.raises(ValueError, match="not a repair summary"):
        panels.load_repairs(tmp_path)


def test_the_patch_text_is_escaped(tmp_path):
    # Breaks if a patch that adds markup to a prompt reaches the page as markup.
    shutil.copytree(REPAIR_OUT, tmp_path / "r")
    patch = tmp_path / "r" / "repair-carry_rules.patch"
    patch.write_text(patch.read_text() + "+<img src=x onerror=alert(1)>\n")

    page = panels.render_patch_panel(panels.load_repairs(tmp_path / "r"))

    assert "<img" not in page
    assert "+&lt;img src=x onerror=alert(1)&gt;" in page


# --- the coverage panel ------------------------------------------------------------------------


def test_coverage_counts_from_the_honesty_block_and_the_pooled_runs(rich):
    # Breaks if a count is taken from the wrong place. results_rich.json: 5 declared, 4 with a
    # check, runs pooled for C-001 (checked) and C-002 (not), recall not measured.
    text = text_of(panels.render_coverage(rich, None))

    assert "Declared in the ledger 5" in text
    assert "With a compiled check 4 of 5" in text
    assert "Exercised: at least one pooled run 2 of 5" in text
    assert "Exercised and checked: the only ones with a verdict 1 of 5" in text
    assert "Extraction recall not measured" in text
    assert 'C-002 Never run <script>alert("x")</script> & friends.' in text
    assert "3 declared constraints were never exercised" in text


def test_coverage_with_the_ledger_lists_unchecked_and_never_exercised_rules(rich):
    # Breaks if a rule that never ran, or one with no check, is left out of the lists.
    ledger = load_ledger_yaml(
        ledger_text(checked=["C-001", "C-003", "C-004", "C-005"], unchecked=["C-002"])
    )
    page = panels.render_coverage(rich, ledger)

    unchecked, never = page.split("Never exercised")
    assert "<code>C-002</code> Rule C-002 text." in unchecked
    assert "C-001" not in unchecked.split("cannot be graded")[1]
    assert [m for m in re.findall(r"<code>(C-\d+)</code>", never)] == ["C-003", "C-004", "C-005"]
    assert "may not be the same ledger" not in page


def test_coverage_warns_when_the_ledger_is_not_the_one_the_results_counted(rich):
    # Breaks if a ledger of the wrong size is listed as if it explained the numbers.
    ledger = load_ledger_yaml(ledger_text(checked=["C-001"], unchecked=["C-002"]))

    text = text_of(panels.render_coverage(rich, ledger))

    assert "The ledger lists 2 constraints but results.json counts 5" in text


def test_coverage_prints_a_measured_recall(rich):
    # Breaks if a measured recall is shown as "not measured" or as a raw fraction.
    rich["honesty"]["extraction_recall"] = 0.5

    assert "Extraction recall 50%" in text_of(panels.render_coverage(rich, None))


# --- the page ----------------------------------------------------------------------------------


def test_unknown_inconclusive_reasons_are_listed_with_their_counts(rich):
    # Breaks if a reason added by another branch (here run_error) is dropped or hidden: the
    # header must list any key under runs.inconclusive.
    rich["runs"]["inconclusive"]["run_error"] = 4
    rich["runs"]["inconclusive"]["killed_midway"] = 2

    text = text_of(site.render(rich))

    assert "run error 4" in text
    assert "killed midway 2" in text
    assert "not comparable 9" in text


def test_the_page_has_no_trace_panel_unless_runs_are_given(rich):
    # Breaks if the panels appear (or crash) when only results.json is given.
    page = site.render(rich)

    assert "Trace at the violating action" not in page
    assert "no violating mutated run" not in page
    assert "No repair run yet." in text_of(page)
    assert "Coverage" in text_of(page)


def test_the_trace_panel_sits_in_the_right_boundary_section(rich):
    # Breaks if the exemplar is attached to the wrong constraint card or boundary: only
    # C-001 at the delegation has one; its return boundary and C-002 get the "none" note.
    page = site.render(rich, trace=panels.load_trace_evidence([RUN_FOLDERS]))

    c001 = re.search(r'<article class="constraint" id="c0-c-001".*?</article>', page, re.S).group(0)
    sections = re.findall(r'<section class="bnd".*?</section>', c001, re.S)
    assert len(sections) == 2
    assert "Trace at the violating action" in sections[0]
    assert "no violating mutated run" in sections[1]
    c002 = re.search(r'<article class="constraint" id="c1-c-002".*?</article>', page, re.S).group(0)
    assert "Trace at the violating action" not in c002


def test_main_writes_the_page_with_every_panel(tmp_path):
    out = tmp_path / "site" / "index.html"
    ledger = tmp_path / "nemogate.yaml"
    ledger.write_text(ledger_text(checked=["C-001"], unchecked=["C-002"]))

    code = site.main(
        [
            "--results", str(FIXTURES / "results_rich.json"),
            "--out", str(out),
            "--runs", str(RUN_FOLDERS),
            "--repair", str(REPAIR_OUT),
            "--ledger", str(ledger),
        ]
    )  # fmt: skip

    page = out.read_text()
    assert code == 0
    assert "Trace at the violating action" in page
    assert "carry_rules" in page
    assert "may not be the same ledger" in page


def test_main_refuses_a_repair_dir_that_does_not_exist(tmp_path, capsys):
    # Breaks if a typo in --repair silently renders "no repair run yet".
    code = site.main(
        [
            "--results", str(FIXTURES / "results_rich.json"),
            "--out", str(tmp_path / "x.html"),
            "--repair", str(tmp_path / "nope"),
        ]
    )  # fmt: skip

    assert code == 2
    assert "report.site:" in capsys.readouterr().err
    assert not (tmp_path / "x.html").exists()


def test_the_page_is_identical_on_two_renders(rich):
    # Breaks if run-folder order or a timestamp leaks into the output.
    a = site.render(copy.deepcopy(rich), trace=panels.load_trace_evidence([RUN_FOLDERS]))
    b = site.render(
        copy.deepcopy(rich),
        trace=panels.load_trace_evidence(sorted(RUN_FOLDERS.iterdir(), reverse=True)),
    )

    assert a == b
