"""The repair agent (SPEC C5), offline: stub models and an injected re-run, no credit spent."""

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml
from langchain_core.messages import AIMessage

from common.llm import get_model
from repair.__main__ import main
from repair.evidence import Evidence, load_evidence
from repair.guards import (
    MAX_ADDED_TOKENS,
    GuardViolation,
    check_append_only,
    check_editable,
    check_edits,
)
from repair.loop import (
    PENDING_REASON,
    Rates,
    pending_result,
    rates,
    repair_once,
    stopped_result,
    write_report,
)
from repair.menu import MENU, get_fix
from repair.patch import (
    WRITE_PROMPT,
    Edit,
    Patch,
    anchor_hints,
    apply_edits,
    unified_diff,
    write_patch,
)
from repair.plan import PLAN_PROMPT, BudgetExceeded, CallBudget, FixChoice, choose_fix
from repair.worktree import WorktreeRerun
from report.panels import JUDGED_RE, load_repairs
from schemas.run import RunRecord

STAMP = {
    "arm": "mutated",
    "operator": "drop",
    "model_id": "m",
    "safe_config_hash": "h",
    "ledger_hash": "l",
}
LEDGER = {
    "version": 1,
    "constraints": [
        {
            "id": "C-001",
            "text": "Keep out of vendor/",
            "kind": "constraint",
            "source": "user",
            "authority": "binding",
            "scope": ["worker"],
            "origin": "declared",
            "check": {"type": "path_forbidden", "glob": "vendor/**", "ops": ["write"]},
        }
    ],
}
PROMPTS = 'LEAD = """You lead a team. Delegate the work."""\nWORKER = """You write code."""\n'


def record(run_id, violated, success=True, task="X1"):
    return RunRecord.model_validate(
        {
            "run_id": run_id,
            "task_id": task,
            "arm": "mutated",
            "operator": "drop",
            "constraint_id": "C-001",
            "boundary": "delegation:lead->worker",
            "violations": ["C-001"] if violated else [],
            "task_success": success,
            "tokens": {"helper": 0, "sut": 0, "planner": 0},
            "cost_usd": 0.01,
            "checkpoint_id": None,
            "contree_image": None,
            "model_ids": {"helper": "a", "sut": "b", "planner": "c"},
            "safe_config_hash": "h",
            "ledger_hash": "l",
            "repo_sha": "s",
            "lockfile_hash": "k",
        }
    )


def run_folder(root: Path, run_id: str, violated: bool) -> None:
    folder = root / run_id
    folder.mkdir(parents=True)
    (folder / "ledger.yaml").write_text(yaml.safe_dump(LEDGER), encoding="utf-8")
    (folder / "record.json").write_text(record(run_id, violated).model_dump_json(), "utf-8")
    boundary = "delegation:lead->worker"
    events = [
        {**STAMP, "run_id": run_id, "step": 0, "agent": "lead", "type": "boundary_out",
         "boundary": boundary, "content": "Add it. Keep out of vendor/."},
        {**STAMP, "run_id": run_id, "step": 1, "agent": "worker", "type": "boundary_in",
         "boundary": boundary, "content": "Add it."},
    ]  # fmt: skip
    (folder / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", "utf-8")


def structured(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "id": "s1", "args": args}])


EVIDENCE = Evidence(
    constraint_id="C-001",
    rule="Keep out of vendor/",
    check_type="path_forbidden",
    boundary="delegation:lead->worker",
    runs=4,
    violated=3,
    example_sent="Add it. Keep out of vendor/.",
    example_received="Add it.",
)
CHOICE = {"fix_id": "carry_rules", "agent": "lead", "rationale": "the rule is lost on handoff"}
EDIT = {
    "file": "app/prompts.py",
    "old_string": "Delegate the work.",
    "new_string": "Delegate the work. Copy every rule from the user verbatim into each delegation.",
}


# ---- menu -----------------------------------------------------------------------------------


def test_the_menu_is_closed_and_generic():
    assert [f.id for f in MENU] == [
        "carry_rules",
        "require_output_format",
        "no_rule_compression",
        "check_reply_against_rules",
    ]
    with pytest.raises(KeyError):
        get_fix("rewrite_everything")
    text = " ".join(f.instruction for f in MENU) + PLAN_PROMPT + WRITE_PROMPT
    for word in ("migrations", "RISK", "planner", "coder", "reviewer", "http_get"):
        assert word not in text


# ---- evidence -------------------------------------------------------------------------------


def test_load_evidence_summarises_the_constraints_runs(tmp_path):
    run_folder(tmp_path, "r-1", violated=True)
    run_folder(tmp_path, "r-2", violated=False)
    ev = load_evidence(tmp_path, "C-001")
    assert (ev.rule, ev.check_type, ev.boundary) == (
        "Keep out of vendor/",
        "path_forbidden",
        "delegation:lead->worker",
    )
    assert (ev.runs, ev.violated) == (2, 1)
    assert (ev.example_sent, ev.example_received) == ("Add it. Keep out of vendor/.", "Add it.")


def test_load_evidence_with_no_runs_for_the_constraint_raises(tmp_path):
    run_folder(tmp_path, "r-1", violated=True)
    with pytest.raises(ValueError, match="C-009"):
        load_evidence(tmp_path, "C-009")


# ---- plan -----------------------------------------------------------------------------------


def test_choose_fix_returns_a_menu_fix_for_a_known_agent():
    model = get_model("planner", stub_responses=[structured("FixChoice", CHOICE)])
    choice = choose_fix(model, EVIDENCE, ["lead", "worker"], CallBudget())
    assert (choice.fix_id, choice.agent) == ("carry_rules", "lead")


def test_choose_fix_refuses_an_agent_the_team_lacks():
    bad = {**CHOICE, "agent": "auditor"}
    model = get_model("planner", stub_responses=[structured("FixChoice", bad)])
    with pytest.raises(ValueError, match="auditor"):
        choose_fix(model, EVIDENCE, ["lead", "worker"], CallBudget())


def test_call_budget_allows_three_ultra_calls_per_iteration():
    # SPEC Budget rules: Ultra, at most 3 calls per repair iteration.
    budget = CallBudget()
    for _ in range(3):
        budget.spend()
    with pytest.raises(BudgetExceeded):
        budget.spend()


def test_fix_choice_only_accepts_menu_ids():
    with pytest.raises(ValueError):
        FixChoice.model_validate({**CHOICE, "fix_id": "delete_the_rule"})


# ---- guards ---------------------------------------------------------------------------------

FILES = {"app/prompts.py": PROMPTS}


def edit(**over):
    return Edit.model_validate({**EDIT, **over})


def test_a_good_insert_passes_the_guards():
    check_edits([edit()], FILES)


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        (edit(file="app/secret.py"), "editable"),
        (edit(new_string="Hand it over."), "insert"),  # removes the instruction
        (edit(old_string="Nowhere in the file."), "exactly once"),
        (edit(old_string='"""'), "exactly once"),  # ambiguous: occurs several times
        (edit(old_string=""), "exactly once"),
    ],
    ids=["not-editable", "not-insert-only", "missing", "ambiguous", "empty"],
)
def test_guards_reject_unsafe_edits(bad, match):
    with pytest.raises(GuardViolation, match=match):
        check_edits([bad], FILES)


@pytest.mark.parametrize(
    "path",
    [
        "nemogate.yaml",  # the ledger (its regexes are predicates)
        "config.yaml",
        "pyproject.toml",
        "uv.lock",
        "schemas/ledger.py",
        "oracle/evaluate.py",
        "gate/middleware.py",
        "ledger/loader.py",
        "replay/runner.py",
        "repair/guards.py",
        "common/llm.py",
        "app/tests/test_api.py",
        "app/test_api.py",
        "app/conftest.py",
        "app/tasks/X1/task.yaml",
        "app/tasks/X1/check_x1.py",
        "app/check_x1.py",
        ".env",
        ".github/CODEOWNERS",
        "docs/SPEC.md",
    ],
)
def test_protected_paths_can_never_be_edited(path):
    # Rule 8 / SPEC risk "the repair agent games the metric": the ledger, config, pipeline,
    # tests, tasks and check scripts are off limits even if the caller lists them as editable.
    files = {path: "old text\n"}
    bad = Edit(file=path, old_string="old text", new_string="old text|.*")
    with pytest.raises(GuardViolation, match="protected"):
        check_edits([bad], files)
    with pytest.raises(GuardViolation, match="protected"):
        check_editable([path])


def test_app_prompt_files_are_editable():
    check_editable(["app/prompts.py", "app/team.py"])


def test_overlapping_edits_are_checked_in_order():
    # The second edit's old_string occurs once in the original but twice after the first edit
    # inserts a copy of it; checking against the original text would wrongly pass it.
    first = edit(new_string="Delegate the work. You write code.")
    second = Edit(
        file="app/prompts.py", old_string="You write code.", new_string="You write code. X"
    )
    with pytest.raises(GuardViolation, match="exactly once"):
        check_edits([first, second], FILES)


def test_token_ceiling_rejects_a_patch_that_dumps_text():
    # SPEC risk: the repair agent games the metric by dumping full history into the prompt.
    flood = "Delegate the work." + " x" * (MAX_ADDED_TOKENS * 4)
    with pytest.raises(GuardViolation, match="token ceiling"):
        check_edits([edit(new_string=flood)], FILES)


def test_ledger_is_append_only_to_the_repair_agent():
    check_append_only("version: 1\n", "version: 1\n# appended\n")
    with pytest.raises(GuardViolation, match="append-only"):
        check_append_only("a: 1\nb: 2\n", "a: 1\n")


# ---- patch ----------------------------------------------------------------------------------


def test_write_patch_asks_super_for_edits_and_names_the_fix():
    model = get_model("sut", stub_responses=[structured("EditList", {"edits": [EDIT]})])
    edits = write_patch(model, get_fix("carry_rules"), FixChoice(**CHOICE), EVIDENCE, FILES)
    assert edits == [Edit(**EDIT)]


def test_apply_edits_and_diff():
    after = apply_edits(FILES, [Edit(**EDIT)])
    assert "Copy every rule" in after["app/prompts.py"]
    diff = unified_diff(FILES, after)
    assert diff.startswith("--- a/app/prompts.py\n+++ b/app/prompts.py\n")
    assert "+LEAD = " in diff and "Copy every rule" in diff


# ---- loop -----------------------------------------------------------------------------------


def test_rates_count_runs_violations_and_successes():
    rs = [record("a", True, True), record("b", False, False), record("c", False, True)]
    r = rates(rs, "C-001")
    assert (r.runs, r.violated, r.succeeded) == (3, 1, 2)
    assert (round(r.cvr, 3), round(r.success, 3)) == (0.333, 0.667)


def models():
    planner = get_model("planner", stub_responses=[structured("FixChoice", CHOICE)])
    writer = get_model("sut", stub_responses=[structured("EditList", {"edits": [EDIT]})])
    return planner, writer


BASELINE = [record("b1", True), record("b2", True), record("b3", False)]


@pytest.mark.parametrize(
    ("after", "accepted"),
    [
        ([record("p1", False), record("p2", False), record("p3", True)], True),
        ([record("p1", True), record("p2", True), record("p3", False)], False),  # no CVR drop
        ([record("p1", False, False), record("p2", False, False), record("p3", False)], False),
    ],
    ids=["fewer-violations", "same-cvr", "success-fell"],
)
def test_repair_once_accepts_only_a_strict_cvr_drop_without_losing_success(after, accepted):
    planner, writer = models()
    seen = []

    def rerun(patch):
        seen.append(patch)
        return after

    result = repair_once(
        evidence=EVIDENCE,
        baseline=BASELINE,
        files=FILES,
        agents=["lead", "worker"],
        planner_model=planner,
        writer_model=writer,
        rerun=rerun,
    )
    assert result.accepted is accepted
    assert seen[0].files_after["app/prompts.py"].count("Copy every rule") == 1
    assert result.before == Rates(3, 2, 3)


def test_repair_once_rejects_a_guard_failure_twice_without_rerunning():
    planner = get_model("planner", stub_responses=[structured("FixChoice", CHOICE)])
    unsafe = {**EDIT, "new_string": "Hand it over."}
    writer = get_model("sut", stub_responses=[structured("EditList", {"edits": [unsafe]})] * 2)

    def rerun(patch):
        raise AssertionError("must not re-run a patch that failed the guards")

    result = repair_once(
        evidence=EVIDENCE,
        baseline=BASELINE,
        files=FILES,
        agents=["lead", "worker"],
        planner_model=planner,
        writer_model=writer,
        rerun=rerun,
    )
    assert result.accepted is False
    assert result.reason.startswith("guard:") and result.reason.endswith("(2 tries)")
    assert result.after is None


def test_a_patch_refused_by_a_guard_is_written_once_more_with_the_reason():
    # Live 2026-10-16: Super's insert used an anchor that occurs more than once in the prompts
    # file, the guard refused it and the iteration ended with no re-run. The writer now gets the
    # guard's reason and its own edits back, once, and the guards check the new edits again.
    planner = get_model("planner", stub_responses=[structured("FixChoice", CHOICE)])
    twice = {"file": "app/prompts.py", "old_string": "\n", "new_string": "\nCopy every rule.\n"}
    writer = Spy(
        get_model(
            "sut",
            stub_responses=[
                structured("EditList", {"edits": [twice]}),
                structured("EditList", {"edits": [EDIT]}),
            ],
        )
    )
    seen = []

    def rerun(patch):
        seen.append(patch)
        return [record(f"p{i}", False) for i in range(3)]

    result = repair_once(
        evidence=EVIDENCE,
        baseline=BASELINE,
        files=FILES,
        agents=["lead", "worker"],
        planner_model=planner,
        writer_model=writer,
        rerun=rerun,
    )
    assert result.accepted is True and len(seen) == 1
    first, second = writer.prompts
    assert "A guard rejected" not in first
    assert second.startswith(first)
    assert "must occur exactly once in app/prompts.py (it occurs" in second
    assert '"old_string": "\\n"' in second  # its own rejected edit, shown back


# A prompts module shaped like the reference team's: a module-level triple-quoted string whose
# first line ends in a backslash continuation (`"""\` + newline), body lines ending the same way.
SHAPED = (
    '"""Prompts."""\n\n'
    'LEAD_PROMPT = """\\\n'
    "You lead a small software team working on the repository at /repo. You do not edit files \\\n"
    "yourself. Understand the request, then delegate the work with the task tool, because the \\\n"
    "person you delegate to cannot see this conversation. Finish with a short summary."
    '"""\n'
)
SHAPED_FILES = {"app/prompts.py": SHAPED}
BODY_LINE = (
    "yourself. Understand the request, then delegate the work with the task tool, because the \\"
)
# The anchor Super sent on both tries in the live round (the guard's own message, unescaped).
LIVE_BAD_ANCHOR = 'LEAD_PROMPT = ""\\\nYou lead a small software team working on the repository at'


def shaped_edit(old_string, new_string=None):
    return Edit(
        file="app/prompts.py",
        old_string=old_string,
        new_string=new_string or old_string + "\nCopy every rule into each task.",
    )


LAST_LINE = "person you delegate to cannot see this conversation. Finish with a short summary."


def test_a_whole_body_line_of_a_triple_quoted_prompt_is_a_valid_anchor():
    # Catches a fix that tightens the guard so far that the reliable anchor (a whole line inside
    # the prompt text) is refused too: after the last line, which has no continuation, and before
    # any line.
    check_edits([shaped_edit(LAST_LINE)], SHAPED_FILES)
    check_edits([shaped_edit(BODY_LINE, "Copy every rule into each task. " + BODY_LINE)],
                SHAPED_FILES)  # fmt: skip


def test_an_insert_after_a_backslash_continuation_is_refused():
    # Live repair round 2: the writer anchored on a line ending in a backslash continuation and
    # added "\nCopy ...". At runtime backslash-newline joins the lines, so the prompt read
    # "...because the Copy every rule into each task." with the sentence cut in two.
    with pytest.raises(GuardViolation, match="right after a backslash"):
        check_edits([shaped_edit(BODY_LINE)], SHAPED_FILES)


@pytest.mark.parametrize(
    ("old", "new", "ok"),
    [
        ("Delegate the work.", "Delegate the work. Copy every rule.", True),
        ("Delegate the work.", "Delegate the work.\nCopy every rule.", True),
        ("Delegate the work.", "Delegate the work.Copy every rule.", False),  # the live glue
        ("Delegate the work.", "Copy every rule.Delegate the work.", False),
        ("You lead a team", "You lead a team, carefully", True),  # continues the sentence
    ],
    ids=["space", "newline", "glued-after", "glued-before", "continues"],
)
def test_an_insert_must_not_run_into_the_text_around_it(old, new, ok):
    edits = [Edit(file="app/prompts.py", old_string=old, new_string=new)]
    if ok:
        check_edits(edits, FILES)
    else:
        with pytest.raises(GuardViolation, match="run into its neighbour"):
            check_edits(edits, FILES)


def test_the_assignment_line_with_wrong_quoting_is_still_refused_by_the_guard():
    # Catches a fix that loosens the exactly-once match (e.g. fuzzy anchors) so the live round's
    # bad anchor, two quotes and an escaped backslash instead of `"""\` + newline, would pass.
    with pytest.raises(GuardViolation, match=r"occurs 0 times: 'LEAD_PROMPT = \"\"\\\\\\nYou lead"):
        check_edits([shaped_edit(LIVE_BAD_ANCHOR)], SHAPED_FILES)


def test_the_write_prompt_steers_the_anchor_to_a_whole_line_inside_the_text():
    # Catches the writer being left free to anchor on the `NAME = """\` line, whose quotes and
    # backslash it mistypes.
    assert "whole line" in WRITE_PROMPT
    assert "opens or closes" in WRITE_PROMPT


def test_anchor_hints_offer_the_closest_whole_lines_and_never_the_string_delimiter_line():
    # Catches a retry that tells the writer it was wrong but not what the file really says: the
    # live anchor had 0 matches because of a few quote characters.
    hints = anchor_hints([shaped_edit(LIVE_BAD_ANCHOR)], SHAPED_FILES)
    first_body = "You lead a small software team working on the repository at /repo."
    assert first_body in hints
    assert 'LEAD_PROMPT = """' not in hints


def test_anchor_hints_skip_edits_whose_anchor_already_occurs_once():
    # Catches hints for an edit that was fine: they would send the writer off a good anchor.
    assert anchor_hints([shaped_edit(BODY_LINE)], SHAPED_FILES) == ""


def test_anchor_hints_fall_back_to_the_first_unique_body_lines_when_nothing_is_close():
    # Catches an empty hint list when the anchor resembles nothing in the file.
    hints = anchor_hints([shaped_edit("zzz qqq xxx")], SHAPED_FILES)
    assert BODY_LINE in hints


def test_the_retry_after_a_bad_anchor_shows_the_guard_error_and_the_real_lines():
    # Live 2026-10-19: both tries sent the same unmatched anchor. The retry now carries the
    # guard's text and the closest whole lines of the file, and the second edit, anchored on
    # one of them, produces the patch and is re-run.
    planner = get_model("planner", stub_responses=[structured("FixChoice", CHOICE)])
    # The good edit inserts before the hinted line: after it, the line's backslash continuation
    # would glue the addition into the sentence (the round-2 gap, refused by the guard).
    bad = shaped_edit(LIVE_BAD_ANCHOR)
    good = shaped_edit(BODY_LINE, "Copy every rule into each task. " + BODY_LINE)
    writer = Spy(
        get_model(
            "sut",
            stub_responses=[
                structured("EditList", {"edits": [bad.model_dump()]}),
                structured("EditList", {"edits": [good.model_dump()]}),
            ],
        )
    )
    seen = []

    def rerun(patch):
        seen.append(patch)
        return [record(f"p{i}", False) for i in range(3)]

    result = repair_once(
        evidence=EVIDENCE,
        baseline=BASELINE,
        files=SHAPED_FILES,
        agents=["lead", "worker"],
        planner_model=planner,
        writer_model=writer,
        rerun=rerun,
    )
    first, second = writer.prompts
    assert "occurs 0 times" in second
    assert "Whole lines of app/prompts.py" not in first
    assert "Whole lines of app/prompts.py" in second and BODY_LINE in second
    assert result.accepted is True and len(seen) == 1
    assert seen[0].files_after["app/prompts.py"].count("Copy every rule into each task.") == 1


def test_a_writer_that_repeats_the_bad_anchor_is_still_rejected_without_a_rerun():
    # Catches hints that make a bad anchor pass: the same wrong anchor twice must end in the
    # guard's rejection, as in the live round, and never reach a re-run.
    planner = get_model("planner", stub_responses=[structured("FixChoice", CHOICE)])
    bad = shaped_edit(LIVE_BAD_ANCHOR).model_dump()
    writer = get_model("sut", stub_responses=[structured("EditList", {"edits": [bad]})] * 2)

    def rerun(patch):
        raise AssertionError("must not re-run a patch that failed the guards")

    result = repair_once(
        evidence=EVIDENCE,
        baseline=BASELINE,
        files=SHAPED_FILES,
        agents=["lead", "worker"],
        planner_model=planner,
        writer_model=writer,
        rerun=rerun,
    )
    assert result.accepted is False and result.after is None
    assert result.reason.startswith("guard: old_string must occur exactly once")
    assert result.reason.endswith("(2 tries)")


class Spy:
    """A structured-output model that records each prompt."""

    def __init__(self, model):
        self.model, self.prompts = model, []

    def with_structured_output(self, schema):
        bound, spy = self.model.with_structured_output(schema), self

        class _Recorder:
            def invoke(self, prompt):
                spy.prompts.append(prompt)
                return bound.invoke(prompt)

        return _Recorder()


def test_repair_once_rejects_an_empty_patch_without_rerunning():
    # A patch with no edits would re-run unchanged code and spend the repair bucket for nothing.
    planner = get_model("planner", stub_responses=[structured("FixChoice", CHOICE)])
    writer = get_model("sut", stub_responses=[structured("EditList", {"edits": []})] * 2)

    def rerun(patch):
        raise AssertionError("must not re-run an empty patch")

    result = repair_once(
        evidence=EVIDENCE,
        baseline=BASELINE,
        files=FILES,
        agents=["lead", "worker"],
        planner_model=planner,
        writer_model=writer,
        rerun=rerun,
    )
    assert (result.accepted, result.reason) == (
        False,
        "guard: the patch makes no change (2 tries)",
    )


def test_too_few_runs_is_never_accepted():
    # Ritam's review: one clean patched run must not beat a noisy baseline.
    planner, writer = models()
    result = repair_once(
        evidence=EVIDENCE,
        baseline=BASELINE,
        files=FILES,
        agents=["lead", "worker"],
        planner_model=planner,
        writer_model=writer,
        rerun=lambda patch: [record("p1", False)],
    )
    assert result.accepted is False
    assert "too few runs" in result.reason


def test_write_report_writes_the_patch_and_a_summary(tmp_path):
    planner, writer = models()
    result = repair_once(
        evidence=EVIDENCE,
        baseline=BASELINE,
        files=FILES,
        agents=["lead", "worker"],
        planner_model=planner,
        writer_model=writer,
        rerun=lambda patch: [record(f"p{i}", False) for i in range(3)],
    )
    patch_path, summary_path = write_report(result, tmp_path)
    assert patch_path.read_text().startswith("--- a/app/prompts.py")
    summary = summary_path.read_text()
    assert "carry_rules" in summary and "accepted" in summary.lower()


# ---- worktree re-run ------------------------------------------------------------------------


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_worktree_rerun_commits_the_patch_and_runs_the_runner_there(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "prompts.py").write_text(PROMPTS, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed")
    calls = []

    def fake_runner(cmd, cwd, env):
        calls.append((cmd, Path(cwd), env.get("NEMOGATE_LIVE")))
        out = Path(cwd) / "runs" / "r-p1"
        out.mkdir(parents=True)
        (out / "record.json").write_text(record("p1", False).model_dump_json(), "utf-8")

    rerun = WorktreeRerun(
        repo_dir=repo,
        worktrees_dir=tmp_path / "trees",
        tasks=["X1"],
        arms=["mutated"],
        operators=["drop"],
        n=1,
        run_cmd=fake_runner,
    )
    after = apply_edits(FILES, [Edit(**EDIT)])
    records = rerun(Patch(fix_id="carry_rules", edits=[Edit(**EDIT)], files_after=after))
    assert [r.run_id for r in records] == ["p1"]
    (cmd, cwd, live) = calls[0]
    # The runner only claims the commit it runs from, so it runs in the patched worktree.
    assert cmd[cmd.index("--repo-ref") + 1] == "HEAD"
    assert cmd[cmd.index("--n") + 1] == "1" and "--yes" in cmd
    assert live == "1" and cwd.parent == tmp_path / "trees"
    assert "Copy every rule" in (cwd / "app" / "prompts.py").read_text()
    log = git(cwd, "log", "-1", "--format=%s%n%b").stdout
    assert "carry_rules" in log and "Co-Authored-By" not in log
    assert git(repo, "status", "--porcelain").stdout == ""  # the main checkout is untouched


def test_discard_last_removes_a_rejected_patchs_worktree_and_branch(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "prompts.py").write_text(PROMPTS, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed")
    rerun = WorktreeRerun(
        repo_dir=repo,
        worktrees_dir=tmp_path / "trees",
        tasks=["X1"],
        arms=["mutated"],
        operators=["drop"],
        n=1,
        run_cmd=lambda *a: None,
    )
    after = apply_edits(FILES, [Edit(**EDIT)])
    rerun(Patch(fix_id="carry_rules", edits=[Edit(**EDIT)], files_after=after))
    assert len(list((tmp_path / "trees").iterdir())) == 1
    rerun.discard_last()
    assert list((tmp_path / "trees").iterdir()) == []
    assert "repair/" not in git(repo, "branch", "--list").stdout


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py", "app/../../outside.py"])
def test_worktree_rerun_refuses_paths_outside_the_worktree(tmp_path, path):
    # Breaks if an absolute or escaping path lets the patch land in the main checkout.
    rerun = WorktreeRerun(
        repo_dir=tmp_path,
        worktrees_dir=tmp_path / "trees",
        tasks=["X1"],
        arms=["mutated"],
        operators=["drop"],
        n=1,
        run_cmd=lambda *a: None,
    )
    with pytest.raises(ValueError, match="repo-relative"):
        rerun(Patch(fix_id="carry_rules", edits=[], files_after={path: "x"}))


def test_baseline_records_filter_to_one_arm_and_operator(tmp_path):
    from repair.evidence import load_records

    run_folder(tmp_path, "r-1", violated=True)
    data = {**record("r-2", False).model_dump(mode="json"), "arm": "natural", "operator": "none"}
    natural = RunRecord.model_validate(data)
    (tmp_path / "r-2").mkdir()
    (tmp_path / "r-2" / "record.json").write_text(natural.model_dump_json(), "utf-8")
    both = load_records(tmp_path, "C-001", "X1")
    only = load_records(tmp_path, "C-001", "X1", arm="mutated", operator="drop")
    assert ([r.run_id for _, r in both], [r.run_id for _, r in only]) == (["r-1", "r-2"], ["r-1"])


# ---- CLI ------------------------------------------------------------------------------------


def test_cli_refuses_without_the_live_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NEMOGATE_LIVE", raising=False)
    code = main(
        ["--runs", str(tmp_path), "--constraint", "C-001", "--task", "X1",
         "--editable", "app/prompts.py", "--agents", "lead,worker", "--n", "1"]
    )  # fmt: skip
    assert code == 2
    assert "NEMOGATE_LIVE" in capsys.readouterr().err


# The CLI tests patch the reference app's prompt file, read from HEAD of this repo.
PROMPT_FILE = "refapp/prompts.py"
PROMPT_ANCHOR = "You lead a small software team"


class FakeRerun:
    def __init__(self, records):
        self.records, self.patches, self.discarded = records, [], 0

    def __call__(self, patch):
        self.patches.append(patch)
        return self.records

    def discard_last(self):
        self.discarded += 1


@pytest.fixture
def live_cli(tmp_path, monkeypatch):
    # get_model refuses stub replies while NEMOGATE_LIVE=1, so the stubs are built first and
    # the live flag is set only around main(); the injected models mean nothing is spent.
    monkeypatch.delenv("NEMOGATE_LIVE", raising=False)
    monkeypatch.setenv("NEMOGATE_SPEND_LOG", str(tmp_path / "spend.jsonl"))
    runs = tmp_path / "runs"
    for i, violated in enumerate([True, True, False]):
        run_folder(runs, f"r-{i}", violated=violated)

    def go(*extra, editable=PROMPT_FILE, after=None, rerun=None, writer=None):
        edit_args = {
            "file": PROMPT_FILE,
            "old_string": PROMPT_ANCHOR,
            "new_string": PROMPT_ANCHOR + ". Copy every rule from the user into each delegation",
        }
        planner = get_model("planner", stub_responses=[structured("FixChoice", CHOICE)])
        writer = writer or get_model(
            "sut", stub_responses=[structured("EditList", {"edits": [edit_args]})]
        )
        if rerun == "build":  # let the CLI build its own re-run (the test replaces the class)
            rerun, passed = None, None
        else:
            rerun = rerun or FakeRerun(
                after if after is not None else [record(f"p{i}", False) for i in range(3)]
            )
            passed = rerun
        argv = [
            "--runs", str(runs), "--constraint", "C-001", "--task", "X1",
            "--editable", editable, "--agents", "lead,worker",
            "--out", str(tmp_path / "out"), *extra,
        ]  # fmt: skip
        monkeypatch.setenv("NEMOGATE_LIVE", "1")
        try:
            code = main(argv, planner_model=planner, writer_model=writer, rerun=passed)
        finally:
            monkeypatch.delenv("NEMOGATE_LIVE", raising=False)
        return code, rerun

    return go


def test_cli_without_yes_prints_the_plan_and_runs_nothing(live_cli, capsys):
    # Ritam's review (rule 3): the re-run spends credit, so it needs the CLI's own --yes.
    code, rerun = live_cli("--n", "3")
    assert code == 1 and rerun.patches == []
    out = capsys.readouterr()
    assert "planned re-runs: 3" in out.out and "--yes" in out.err


def test_cli_refuses_fewer_reruns_than_a_verdict_needs(live_cli, capsys):
    # Fewer than MIN_RUNS patched runs can never be accepted, so spending on them is waste.
    code, rerun = live_cli("--n", "2", "--yes")
    assert code == 2 and rerun.patches == []
    assert "at least 3" in capsys.readouterr().err


def test_cli_refuses_more_runs_than_the_repair_bucket_allows(live_cli, capsys):
    code, rerun = live_cli("--n", "100000", "--yes")
    assert code == 2 and rerun.patches == []
    assert "repair bucket" in capsys.readouterr().err


@pytest.mark.parametrize(
    "path", ["nemogate.yaml", "oracle/evaluate.py", "refapp/tests/test_team.py"]
)
def test_cli_refuses_protected_editable_files(live_cli, capsys, path):
    code, rerun = live_cli("--n", "3", "--yes", editable=path)
    assert code == 2 and rerun.patches == []
    assert "protected" in capsys.readouterr().err


def test_cli_accepts_a_good_patch_and_keeps_its_branch(live_cli, tmp_path):
    code, rerun = live_cli("--n", "3", "--yes")
    assert code == 0 and rerun.discarded == 0
    (patch,) = rerun.patches
    # Read from HEAD, so uncommitted edits in this checkout never reach the repair branch.
    head = subprocess.run(
        ["git", "show", f"HEAD:{PROMPT_FILE}"], capture_output=True, text=True, check=True
    ).stdout
    assert (
        patch.files_after[PROMPT_FILE].replace(
            ". Copy every rule from the user into each delegation", ""
        )
        == head
    )
    assert (tmp_path / "out" / "repair-carry_rules.patch").read_text().startswith("--- a/refapp/")


def test_cli_discards_a_rejected_patchs_branch(live_cli):
    same = [record(f"p{i}", i < 2) for i in range(3)]  # 2 of 3 violated: no improvement
    code, rerun = live_cli("--n", "3", "--yes", after=same)
    assert code == 1 and rerun.discarded == 1


def test_the_write_prompt_asks_for_a_separated_addition():
    # The guard refuses a glued insert; the prompt says how to avoid one up front.
    assert "Start your addition with a newline" in " ".join(WRITE_PROMPT.split())
    assert "ends with a backslash" in " ".join(WRITE_PROMPT.split())


# ---- a round that does not finish still leaves a summary -------------------------------------
#
# Live repair round 2 (A, 2026-10-19): the worker's timeout killed `python -m repair` after 18 of
# its 20 re-runs, and because the summary was written only after the re-runs, there was none.


class StoppingRerun(FakeRerun):
    """A re-run that stops with `exc` after `finished` records, as a killed runner does."""

    def __init__(self, exc, finished=(), tree=None):
        super().__init__([])
        self.exc, self.finished = exc, list(finished)
        self.last = (tree, "repair/carry_rules-abc123") if tree else None

    def __call__(self, patch):
        self.patches.append(patch)
        raise self.exc

    def completed(self):
        return self.finished


def stopped_cli(live_cli, rerun):
    """Run the CLI with a re-run that stops. A KeyboardInterrupt that escapes would abort the whole
    test session, so a regression is turned into one failing test."""
    try:
        return live_cli("--n", "3", "--yes", rerun=rerun)
    except KeyboardInterrupt:
        pytest.fail("a KeyboardInterrupt escaped the CLI: the stopped round was not reported")


def summary_of(out: Path) -> str:
    return (out / "repair-carry_rules.md").read_text(encoding="utf-8")


class Probe(FakeRerun):
    """Looks at the output folder while the re-runs are 'running'."""

    def __init__(self, out: Path, records):
        super().__init__(records)
        self.out, self.seen = out, None

    def __call__(self, patch):
        self.seen = (summary_of(self.out), (self.out / "repair-carry_rules.patch").read_text())
        return super().__call__(patch)


def test_the_summary_and_diff_exist_before_the_reruns_start(live_cli, tmp_path):
    probe = Probe(tmp_path / "out", [record(f"p{i}", False) for i in range(3)])
    code, _ = live_cli("--n", "3", "--yes", rerun=probe)
    summary, diff = probe.seen
    assert summary.startswith("# Repair: Rejected\n")  # the only headings the results page reads
    assert PENDING_REASON in summary and "Nothing is accepted" in summary
    assert diff.startswith("--- a/refapp/")  # the patch being tested is already on disk
    assert code == 0
    assert summary_of(tmp_path / "out").startswith("# Repair: Accepted\n")  # then rewritten


@pytest.mark.parametrize(
    ("exc", "why"),
    [
        (KeyboardInterrupt(), "interrupted"),
        (KeyboardInterrupt("SIGTERM"), "stopped by SIGTERM"),
        (subprocess.CalledProcessError(1, ["runner"]), "the runner exited with status 1"),
        (RuntimeError("secret detail"), "failed with RuntimeError"),
    ],
    ids=["ctrl-c", "sigterm", "runner-failed", "other"],
)
def test_a_round_that_stops_is_reported_not_judged_and_keeps_its_runs(
    live_cli, tmp_path, capsys, exc, why
):
    tree = tmp_path / "tree"
    finished = [record("p0", False), record("p1", True)]
    rerun = StoppingRerun(exc, finished, tree)
    code, _ = stopped_cli(live_cli, rerun)
    assert code == 3  # not 0 (accepted), 1 (rejected) or 2 (refused)
    summary = summary_of(tmp_path / "out")
    assert summary.startswith("# Repair: Rejected\n")
    assert f"Not judged: the round stopped ({why}) after 2 of 3 re-runs" in summary
    assert "nothing is accepted" in summary
    assert "1 of 2 (50%) broke the rule, against 2 of 3 (67%)" in summary
    assert f"- Re-runs kept in: `{tree}`" in summary
    assert rerun.discarded == 0  # the finished runs cost credit and are records in the worktree
    out = capsys.readouterr()
    assert "NOT JUDGED" in out.err and f"re-run records kept in: {tree}" in out.out
    assert ("secret detail" in out.err) == isinstance(exc, RuntimeError)  # to the terminal only
    assert "secret detail" not in summary  # nothing a run or model wrote reaches the file


def test_a_round_stopped_before_any_rerun_finished_says_so(live_cli, tmp_path):
    code, _ = stopped_cli(live_cli, StoppingRerun(KeyboardInterrupt()))
    summary = summary_of(tmp_path / "out")
    assert code == 3 and "after 0 of 3 re-runs" in summary
    assert "On the" not in summary and "Re-runs kept" not in summary  # no numbers, no worktree


def test_sigterm_is_handled_like_ctrl_c_and_the_handler_is_put_back(live_cli, tmp_path):
    before = signal.getsignal(signal.SIGTERM)

    class Terminated(FakeRerun):
        def __call__(self, patch):
            handler = signal.getsignal(signal.SIGTERM)
            assert handler not in (before, signal.SIG_DFL, signal.SIG_IGN)
            handler(signal.SIGTERM, None)  # what the signal does, without ending the test run

    code, _ = stopped_cli(live_cli, Terminated([]))
    assert code == 3 and "stopped by SIGTERM" in summary_of(tmp_path / "out")
    assert signal.getsignal(signal.SIGTERM) == before


def test_an_error_before_any_patch_was_tested_is_not_hidden(live_cli, tmp_path):
    class Broken:
        def with_structured_output(self, schema):
            raise RuntimeError("the model is down")

    with pytest.raises(RuntimeError, match="the model is down"):
        live_cli("--n", "3", "--yes", writer=Broken())
    assert not (tmp_path / "out" / "repair-carry_rules.md").exists()  # nothing to report


def test_a_finished_round_is_judged_as_before(live_cli, tmp_path):
    code, rerun = live_cli("--n", "3", "--yes", after=[record(f"p{i}", i < 2) for i in range(3)])
    assert code == 1 and rerun.discarded == 1
    summary = summary_of(tmp_path / "out")
    assert summary.startswith("# Repair: Rejected\n") and "Not judged" not in summary


# ---- what the results page makes of them -----------------------------------------------------


def parsed(tmp_path, result, kept=None):
    out = tmp_path / "page"
    write_report(result, out, kept=kept)
    (view,) = load_repairs(out)
    return view


def pending():
    return pending_result(FixChoice(**CHOICE), [Edit(**EDIT)], "--- a/x\n+++ b/x\n", Rates(3, 2, 3))


def test_the_page_reads_a_pending_and_a_stopped_summary_without_a_judged_table(tmp_path):
    # The page raises on a heading that is not Accepted or Rejected, and prints a "Judged on"
    # table for a result line that has the judged numbers in its fixed wording.
    stopped = stopped_result(pending(), [record("p0", False), record("p1", True)], 3, "x", "C-001")
    for result in (pending(), stopped):
        view = parsed(tmp_path / result.reason[:12].strip(), result, kept=tmp_path / "tree")
        assert view.accepted is False and view.ambiguous is False
        assert view.result.startswith("Not judged")
        assert "kept in" not in view.result  # the kept-folder line sits after `- Diff:`
        assert view.diff is not None and view.fix_id == "carry_rules"
        assert JUDGED_RE.search(view.result.splitlines()[0]) is None


def test_a_stopped_summary_reads_as_one_result_line(tmp_path):
    stopped = stopped_result(pending(), [record("p0", False)], 20, "interrupted", "C-001")
    write_report(stopped, tmp_path)
    text = (tmp_path / "repair-carry_rules.md").read_text()
    assert sum(line.startswith("- Result: ") for line in text.splitlines()) == 1


def test_stopped_result_counts_only_the_rules_runs_and_never_accepts():
    other = record("q0", False).model_copy(update={"constraint_id": "C-002"})
    result = stopped_result(pending(), [record("p0", False), other], 3, "interrupted", "C-001")
    assert result.accepted is False and result.after == Rates(1, 0, 1)
    assert "after 1 of 3 re-runs" in result.reason
    empty = stopped_result(pending(), [], 3, "interrupted", "C-001")
    assert empty.after is None and "after 0 of 3 re-runs" in empty.reason


def test_the_pending_summary_is_never_an_acceptance():
    result = pending()
    assert result.accepted is False and result.after is None
    assert result.reason.startswith("Not judged") and "Nothing is accepted" in result.reason


def test_a_report_write_that_fails_leaves_the_earlier_files_whole(tmp_path, monkeypatch):
    # A kill in the middle of a write must not leave a heading with no result line: the page
    # refuses such a file. Write to a scratch name, then rename: the file is old or new, not half.
    write_report(pending(), tmp_path)
    before = (tmp_path / "repair-carry_rules.md").read_text()
    real = Path.write_text

    def dies(self, *args, **kwargs):
        if ".md" not in self.name:  # the patch file goes first; the crash is in the summary
            return real(self, *args, **kwargs)
        real(self, "# Repair: Rejec", encoding="utf-8")  # half a file, then the process dies
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "write_text", dies)
    with pytest.raises(KeyboardInterrupt):
        write_report(stopped_result(pending(), [], 3, "interrupted", "C-001"), tmp_path)
    monkeypatch.setattr(Path, "write_text", real)
    assert (tmp_path / "repair-carry_rules.md").read_text() == before
    assert len(load_repairs(tmp_path)) == 1  # the scratch file is not mistaken for a summary


# ---- the worktree re-run ---------------------------------------------------------------------


def seeded_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "prompts.py").write_text(PROMPTS, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed")
    return repo


def a_patch():
    return Patch(
        fix_id="carry_rules", edits=[Edit(**EDIT)], files_after=apply_edits(FILES, [Edit(**EDIT)])
    )


def test_completed_reads_the_records_a_stopped_runner_left_and_skips_a_torn_one(tmp_path):
    def killed_runner(cmd, cwd, env):
        for name in ("r-p0", "r-p1"):
            (Path(cwd) / "runs" / name).mkdir(parents=True)
            rec = record(name[2:], False).model_dump_json()
            (Path(cwd) / "runs" / name / "record.json").write_text(rec, "utf-8")
        (Path(cwd) / "runs" / "r-p2").mkdir()
        (Path(cwd) / "runs" / "r-p2" / "record.json").write_text('{"run_id": "p2", "vi', "utf-8")
        raise KeyboardInterrupt

    rerun = WorktreeRerun(
        repo_dir=seeded_repo(tmp_path), worktrees_dir=tmp_path / "trees", tasks=["X1"],
        arms=["mutated"], operators=["drop"], n=3, run_cmd=killed_runner,
    )  # fmt: skip
    assert rerun.completed() == []  # nothing has run yet
    with pytest.raises(KeyboardInterrupt):
        rerun(a_patch())
    assert [r.run_id for r in rerun.completed()] == ["p0", "p1"]
    rerun.discard_last()
    assert rerun.completed() == []


def test_on_start_is_called_with_the_patched_worktree_before_the_runner_starts(tmp_path):
    seen = []

    def runner(cmd, cwd, env):
        seen.append("runner")

    def on_start(tree, branch):
        seen.append(
            ("start", tree, branch, "Copy every rule" in (tree / "app/prompts.py").read_text())
        )

    rerun = WorktreeRerun(
        repo_dir=seeded_repo(tmp_path), worktrees_dir=tmp_path / "trees", tasks=["X1"],
        arms=["mutated"], operators=["drop"], n=1, run_cmd=runner, on_start=on_start,
    )  # fmt: skip
    rerun(a_patch())
    (start, runner_call) = seen
    assert runner_call == "runner" and start[0] == "start"
    assert start[1] == rerun.last[0] and start[2] == rerun.last[1] and start[3] is True


# The default runner: graceful stop, and the exit status of a runner that fails.

CHILD = """
import os, signal, sys, time
marker, ready, mode = sys.argv[1:4]
open(ready + ".pid", "w").write(str(os.getpid()))
if mode == "graceful":
    def stop(signum, frame):
        open(marker, "w").write("terminated")
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
else:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(ready, "w").write("ready")
time.sleep(60)
"""


def interrupt_when_ready(ready: Path):
    """Once the child says it is ready, send the main thread a real SIGINT. (`interrupt_main`
    only sets a flag, which a blocked wait() does not see until the child has exited.)"""
    main = threading.main_thread().ident

    def wait_then_interrupt():
        for _ in range(400):
            if ready.exists():
                break
            threading.Event().wait(0.05)
        signal.pthread_kill(main, signal.SIGINT)

    threading.Thread(target=wait_then_interrupt, daemon=True).start()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_stopped_runner_is_asked_to_stop_not_killed(tmp_path):
    from repair.worktree import _default_run

    marker, ready = tmp_path / "marker", tmp_path / "ready"
    cmd = [sys.executable, "-c", CHILD, str(marker), str(ready), "graceful"]
    interrupt_when_ready(ready)
    with pytest.raises(KeyboardInterrupt):
        _default_run(cmd, tmp_path, dict(os.environ))
    # SIGTERM reached it (SIGKILL could not have written this), so `uv run` could pass it on.
    assert marker.read_text() == "terminated"
    assert not alive(int(Path(f"{ready}.pid").read_text()))


def test_a_runner_that_ignores_the_request_is_killed_after_the_grace_period(tmp_path, monkeypatch):
    import repair.worktree as worktree

    monkeypatch.setattr(worktree, "STOP_GRACE_SECONDS", 0.5)
    ready = tmp_path / "ready"
    cmd = [sys.executable, "-c", CHILD, str(tmp_path / "marker"), str(ready), "stubborn"]
    interrupt_when_ready(ready)
    with pytest.raises(KeyboardInterrupt):
        worktree._default_run(cmd, tmp_path, dict(os.environ))
    assert not alive(int(Path(f"{ready}.pid").read_text()))


def test_the_default_runner_raises_with_the_exit_status_of_a_failed_runner(tmp_path):
    from repair.worktree import _default_run

    _default_run([sys.executable, "-c", "pass"], tmp_path, dict(os.environ))
    with pytest.raises(subprocess.CalledProcessError) as caught:
        _default_run([sys.executable, "-c", "raise SystemExit(7)"], tmp_path, dict(os.environ))
    assert caught.value.returncode == 7


def test_a_real_sigterm_during_a_real_worktree_rerun_leaves_everything_to_recover(
    live_cli, tmp_path
):
    # The live failure, end to end but for the model calls: the runner has finished two re-runs
    # when the process is told to stop. The summary says so, the two records and the worktree
    # and its branch are still there, and the exit status is "not judged".
    head = subprocess.run(
        ["git", "show", f"HEAD:{PROMPT_FILE}"], capture_output=True, text=True, check=True
    ).stdout
    repo = tmp_path / "repo"
    (repo / "refapp").mkdir(parents=True)
    (repo / PROMPT_FILE).write_text(head, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed")

    def runner_told_to_stop(cmd, cwd, env):
        for name in ("p0", "p1"):
            (Path(cwd) / "runs" / f"r-{name}").mkdir(parents=True)
            (Path(cwd) / "runs" / f"r-{name}" / "record.json").write_text(
                record(name, False).model_dump_json(), "utf-8"
            )
        handler = signal.getsignal(signal.SIGTERM)
        if handler in (signal.SIG_DFL, signal.SIG_IGN):  # never send it to a bare test process
            pytest.fail("main() did not install its SIGTERM handler")
        os.kill(os.getpid(), signal.SIGTERM)
        threading.Event().wait(5)  # the handler raises during this wait

    rerun = WorktreeRerun(
        repo_dir=repo, worktrees_dir=tmp_path / "trees", tasks=["X1"], arms=["mutated"],
        operators=["drop"], n=3, run_cmd=runner_told_to_stop,
    )  # fmt: skip
    code, _ = stopped_cli(live_cli, rerun)
    assert code == 3
    summary = summary_of(tmp_path / "out")
    assert "Not judged: the round stopped (stopped by SIGTERM) after 2 of 3 re-runs" in summary
    tree = Path(summary.split("- Re-runs kept in: `")[1].split("`")[0])
    assert sorted(p.parent.name for p in tree.glob("runs/*/record.json")) == ["r-p0", "r-p1"]
    assert git(repo, "branch", "--list", "repair/*").stdout.strip()  # the branch was kept too


def test_the_cli_records_the_worktree_in_the_summary_as_soon_as_it_exists(
    live_cli, tmp_path, monkeypatch
):
    # If the process is killed outright nothing runs after that, so the summary written before
    # the re-runs must already say where the re-run records will be.
    import repair.__main__ as cli

    seen = {}

    class Building:
        def __init__(self, **kwargs):
            seen["on_start"] = kwargs["on_start"]

        def __call__(self, patch):
            seen["on_start"](tmp_path / "tree", "repair/carry_rules-abc123")
            seen["summary"] = summary_of(tmp_path / "out")
            raise SystemExit  # a hard stop, which the CLI does not turn into a report

    monkeypatch.setattr(cli, "WorktreeRerun", Building)
    with pytest.raises(SystemExit):
        live_cli("--n", "3", "--yes", rerun="build")
    text = seen["summary"]
    assert text.startswith("# Repair: Rejected\n") and "Not judged" in text
    assert f"- Re-runs kept in: `{tmp_path / 'tree'}`" in text
    assert text.index("- Diff:") < text.index("- Re-runs kept in:")  # after Diff: not in the result


def test_a_second_ctrl_c_while_the_runner_winds_down_kills_it_at_once():
    from repair.worktree import _stop

    class Runner:
        calls = []

        def terminate(self):
            self.calls.append("terminate")

        def wait(self, timeout=None):
            self.calls.append("wait")
            if timeout is not None:
                raise KeyboardInterrupt  # the user presses Ctrl-C again during the grace wait

        def kill(self):
            self.calls.append("kill")

    runner = Runner()
    _stop(runner)
    assert runner.calls == ["terminate", "wait", "kill", "wait"]
