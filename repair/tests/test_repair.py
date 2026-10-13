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
from repair.guards import MAX_ADDED_TOKENS, GuardViolation, check_append_only, check_edits
from repair.loop import Rates, rates, repair_once, write_report
from repair.menu import MENU, get_fix
from repair.patch import WRITE_PROMPT, Edit, Patch, apply_edits, unified_diff, write_patch
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
            "cost_usd": 0.0,
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


def test_repair_once_rejects_a_guard_failure_without_rerunning():
    planner = get_model("planner", stub_responses=[structured("FixChoice", CHOICE)])
    unsafe = {**EDIT, "new_string": "Hand it over."}
    writer = get_model("sut", stub_responses=[structured("EditList", {"edits": [unsafe]})])

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
    assert result.reason.startswith("guard:")
    assert result.after is None


def test_repair_once_rejects_an_empty_patch_without_rerunning():
    # A patch with no edits would re-run unchanged code and spend the repair bucket for nothing.
    planner = get_model("planner", stub_responses=[structured("FixChoice", CHOICE)])
    writer = get_model("sut", stub_responses=[structured("EditList", {"edits": []})])

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
    assert (result.accepted, result.reason) == (False, "guard: the patch makes no change")


def test_write_report_writes_the_patch_and_a_summary(tmp_path):
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
