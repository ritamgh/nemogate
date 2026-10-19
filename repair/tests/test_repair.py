"""The repair agent (SPEC C5), offline: stub models and an injected re-run, no credit spent."""

import json
import subprocess
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
from repair.loop import Rates, rates, repair_once, write_report
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


def test_a_whole_body_line_of_a_triple_quoted_prompt_is_a_valid_anchor():
    # Catches a fix that tightens the guard so far that the reliable anchor (a whole line inside
    # the prompt text) is refused too.
    check_edits([shaped_edit(BODY_LINE)], SHAPED_FILES)


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
    bad, good = shaped_edit(LIVE_BAD_ANCHOR), shaped_edit(BODY_LINE)
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

    def go(*extra, editable=PROMPT_FILE, after=None):
        edit_args = {
            "file": PROMPT_FILE,
            "old_string": PROMPT_ANCHOR,
            "new_string": PROMPT_ANCHOR + ". Copy every rule from the user into each delegation",
        }
        planner = get_model("planner", stub_responses=[structured("FixChoice", CHOICE)])
        writer = get_model("sut", stub_responses=[structured("EditList", {"edits": [edit_args]})])
        rerun = FakeRerun(
            after if after is not None else [record(f"p{i}", False) for i in range(3)]
        )
        argv = [
            "--runs", str(runs), "--constraint", "C-001", "--task", "X1",
            "--editable", editable, "--agents", "lead,worker",
            "--out", str(tmp_path / "out"), *extra,
        ]  # fmt: skip
        monkeypatch.setenv("NEMOGATE_LIVE", "1")
        try:
            code = main(argv, planner_model=planner, writer_model=writer, rerun=rerun)
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
