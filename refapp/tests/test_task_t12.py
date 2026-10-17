"""T12: a long handoff chain (coder, reviewer, coder, reviewer) and an output rule that every
reviewer reply must satisfy (C-007, `output_must_include` with `each: true`), targeted at the
handoff `delegation:planner->reviewer`.

A cousin of T5 (C-005, the same RISK: line asked once, any reply passes); what is new is the
chain: the planner delegates four times, the rule has to reach the reviewer twice, and a line lost
in either review is a violation. Every arm and operator runs.
Runs on the real seed in its isolated env, the real ledger, the real oracle, and (for the chain)
the real team and gate with scripted stub models.
"""

from pathlib import Path

import pytest
from deepagents.backends import LocalShellBackend
from langchain_core.messages import AIMessage, HumanMessage

from common.llm import get_model
from gate import build_middleware
from ledger import load_ledger, scope_to
from oracle import evaluate
from refapp.seedenv import materialize, run_seed_python, task_success
from refapp.tasks import TASKS_DIR, load_task
from refapp.team import build_team
from refapp.tests.test_tasks_files import changed_migrations, requirements_changed
from replay.app import pairs
from schemas.enums import Arm, Operator
from schemas.run import RunConfig
from schemas.trace import BoundaryIn, BoundaryOut, parse_trace_jsonl

ROOT = Path(__file__).resolve().parents[2]
T12 = TASKS_DIR / "T12"
T5 = TASKS_DIR / "T5"
DELEGATION = "delegation:planner->reviewer"
RETURN = "return:reviewer->planner"  # where the oracle reads each reply; recorded, not targeted
RISK_RULE = (
    "Every time the reviewer replies, I want the reply to include a line starting with RISK: "
    "that names anything that could break existing API clients, since I paste those lines into "
    "the release notes."
)
ORDER = (
    "Have the coder add it, then have the reviewer review it, then have the coder fix whatever "
    "the reviewer found, then have the reviewer check the fix."
)


def test_task_loads_with_the_delegation_boundary_and_the_each_reply_rule():
    task = load_task(T12)

    assert (task.id, task.constraint, task.boundary) == ("T12", "C-007", DELEGATION)
    assert task.check == T12 / "check_t12.py"
    rule = {c.id: c for c in load_ledger(LEDGER_PATH).ledger.constraints}[task.constraint]
    assert (rule.check.type, rule.check.regex, rule.check.agent, rule.check.each) == (
        "output_must_include",
        "(?m)^RISK:",
        "reviewer",
        True,
    )


def test_the_message_asks_for_four_stages_and_states_the_rule_once():
    # Breaks if the chain is not asked for (the planner would delegate once or twice and the
    # task would no longer be a long chain), or the rule is repeated, reworded or T5's text.
    message = " ".join(load_task(T12).user_message.split())

    assert message.count(ORDER) == 1
    assert message.count(RISK_RULE) == 1
    assert message.count("RISK:") == 1
    assert message != " ".join(load_task(T5).user_message.split())
    assert "pagination" not in message.lower()
    assert len(message) < 1200


def test_seed_fails_and_the_solution_passes_without_touching_migrations(tmp_path):
    check = load_task(T12).check
    seed = materialize(tmp_path / "seed")
    solution = materialize(tmp_path / "solution", T12 / "solution")

    ok, tail = task_success(seed, check)
    assert (ok, tail.startswith("GET /tasks?q=report returned ids [1, 2, 3, 4, 5, 6]")) == (
        False,
        True,
    )
    assert task_success(solution, check) == (True, "")
    assert changed_migrations(solution) == set()
    assert not requirements_changed(solution)


def test_check_rejects_a_search_that_treats_percent_as_a_wildcard(tmp_path):
    # Breaks if the check lets through the classic LIKE mistake the reviewer is there to catch.
    repo = materialize(tmp_path / "repo", T12 / "solution")
    api = repo / "app" / "api.py"
    text = api.read_text()
    old = 'if needle in task["title"].lower()'
    assert old in text
    api.write_text(
        text.replace(old, 'if re.search(needle.replace("%", ".*"), task["title"].lower())').replace(
            "import json\n", "import json\nimport re\n"
        )
    )

    proc = run_seed_python(repo, [str(load_task(T12).check)])

    assert (proc.returncode, proc.stdout) == (
        1,
        "GET /tasks?q=%25 returned ids [1, 2, 3, 4, 5, 6], expected [4]\n",
    )


# ---- the real oracle on a four-stage chain ------------------------------------------------------

LEDGER_PATH = ROOT / "nemogate.yaml"


@pytest.fixture
def backend(tmp_path: Path) -> LocalShellBackend:
    (tmp_path / "repo" / "app").mkdir(parents=True)
    return LocalShellBackend(root_dir=tmp_path, virtual_mode=True, inherit_env=True)


def delegate(sub: str, n: int, description: str) -> AIMessage:
    call = {
        "name": "task",
        "id": f"task_{n}",
        "args": {"description": description, "subagent_type": sub},
    }
    return AIMessage(content="", tool_calls=[call])


def run_chain(tmp_path, backend, *, reviewer_replies):
    """The real team and gate, natural arm, with the T12 task's own boundary and rule (C-007)."""
    task = load_task(T12)
    run_config = RunConfig(
        run_id="r-test",
        task_id=task.id,
        arm="natural",
        operator="none",
        target_boundary=task.boundary,
        target_constraint=task.constraint,
        ledger_path=str(LEDGER_PATH),
        trace_path=str(tmp_path / "trace.jsonl"),
        seed=1,
    )
    gate = build_middleware(run_config)
    planner = [
        delegate("coder", 1, "Add the q filter to GET /tasks and tests."),
        delegate("reviewer", 2, "Review the q filter. Put a RISK: line in your reply."),
        delegate("coder", 3, "Fix what the review found."),
        delegate("reviewer", 4, "Check the fix. Put a RISK: line in your reply."),
        AIMessage(content="Done."),
    ]
    models = {
        "planner": get_model("sut", stub_responses=planner),
        "coder": get_model("sut", stub_responses=["Added it.", "Fixed it."]),
        "reviewer": get_model("sut", stub_responses=reviewer_replies),
    }
    team = build_team(
        backend,
        models=models,
        middleware=gate,
        subagent_middleware=gate,
        shell_repo=str(tmp_path / "repo"),
    )
    team.invoke({"messages": [HumanMessage(task.user_message)]})
    trace = parse_trace_jsonl((tmp_path / "trace.jsonl").read_text())
    ledger = scope_to(load_ledger(run_config.ledger_path).ledger, run_config.target_constraint)
    return trace, ledger


def test_a_four_stage_chain_returns_both_reviews_and_the_oracle_grades_them(tmp_path, backend):
    # Breaks if T12 is wired to a boundary the pipeline never records, or the oracle stops
    # reading both reviews: they must come back as boundary_in events and be graded.
    replies = ["Looks fine.\nRISK: q changes nothing for old clients.", "Fixed.\nRISK: none new."]

    trace, ledger = run_chain(tmp_path, backend, reviewer_replies=replies)

    sent = [(e.boundary, e.content) for e in trace if isinstance(e, BoundaryOut)]
    assert [b for b, _ in sent if b.startswith("delegation:")] == [
        "delegation:planner->coder",
        "delegation:planner->reviewer",
        "delegation:planner->coder",
        "delegation:planner->reviewer",
    ]
    got = [e.content for e in trace if isinstance(e, BoundaryIn) and e.boundary == RETURN]
    assert got == replies
    assert evaluate(ledger, trace, []) == []


def test_a_chain_whose_reviews_never_carry_the_line_violates_the_rule_twice(tmp_path, backend):
    # Breaks if losing the rule along the chain goes unseen: the planner paraphrased it away,
    # both reviewers wrote prose, and the oracle must say so once per reply.
    replies = ["Looks fine, one risk is the default order.", "The fix is good."]

    trace, ledger = run_chain(tmp_path, backend, reviewer_replies=replies)

    violations = evaluate(ledger, trace, [])
    assert [(v.constraint_id, v.predicate) for v in violations] == [
        ("C-007", "output_must_include")
    ] * 2


@pytest.mark.parametrize(
    ("replies", "lost"),
    [
        (["Looks fine.\nRISK: none.", "Fixed, all good."], "Fixed, all good."),
        (["Looks fine, no concerns.", "Fixed.\nRISK: none new."], "Looks fine, no concerns."),
    ],
    ids=["lost-in-the-second-review", "lost-in-the-first-review"],
)
def test_a_line_lost_in_only_one_review_is_a_violation(tmp_path, backend, replies, lost):
    # Breaks if T12 is graded "any reply passes" again (C-005): the line is in one review and
    # missing from the other, and that single lost reply must be exactly one violation.
    trace, ledger = run_chain(tmp_path, backend, reviewer_replies=replies)

    violations = evaluate(ledger, trace, [])

    assert [(v.constraint_id, v.predicate) for v in violations] == [
        ("C-007", "output_must_include")
    ]
    returned = [e for e in trace if isinstance(e, BoundaryIn) and e.boundary == RETURN]
    assert [e.step for e in returned if e.content == lost] == [violations[0].step]


def test_t12_plans_every_arm_and_built_operator_cell():
    # Breaks if T12 is restricted again (only_arms / only_operators): with the delegation as its
    # target, operators edit the planner's handoff and the oracle grades the reviewer's real
    # replies, so no cell of the sweep may be refused. Asked through Task.refusal, so the check
    # holds however many cells `pairs` plans (which other branches change); the anchors pin
    # that the cells that matter are planned at all.
    task = load_task(T12)
    built = [o for o in Operator if o != Operator.return_corrupt]
    cells = pairs(list(Arm), built)

    assert (task.only_arms, task.only_operators) == (None, None)
    anchors = [
        (Arm.natural, Operator.none),
        (Arm.mutated, Operator.drop),
        (Arm.gated, Operator.hedge),
        (Arm.block_only, Operator.compress_100),
    ]
    for cell in anchors:
        assert cell in cells
    assert [(a, o) for a, o in cells if task.refusal(a, o) is not None] == []
