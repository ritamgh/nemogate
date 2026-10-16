"""T12: a long handoff chain (coder, reviewer, coder, reviewer) and an output rule graded where the
reviewer's reply comes back to the planner (`return:reviewer->planner`).

Same rule as T5 (C-005, the reviewer's RISK: line), so no ledger entry is added; what is new is
the chain: the planner delegates four times, and the rule has to reach the reviewer twice.
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
from schemas.run import RunConfig
from schemas.trace import BoundaryIn, BoundaryOut, parse_trace_jsonl

ROOT = Path(__file__).resolve().parents[2]
T12 = TASKS_DIR / "T12"
T5 = TASKS_DIR / "T5"
RETURN = "return:reviewer->planner"
RISK_RULE = (
    "Every time the reviewer replies, I want the reply to include a line starting with RISK: "
    "that names anything that could break existing API clients, since I paste those lines into "
    "the release notes."
)
ORDER = (
    "Have the coder add it, then have the reviewer review it, then have the coder fix whatever "
    "the reviewer found, then have the reviewer check the fix."
)


def test_task_loads_with_the_return_boundary_and_the_reviewer_rule():
    task = load_task(T12)

    assert (task.id, task.constraint, task.boundary) == ("T12", "C-005", RETURN)
    assert task.check == T12 / "check_t12.py"


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
    """The real team and gate, natural arm, with the T12 task's own boundary and rule."""
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


def test_a_four_stage_chain_returns_both_reviews_on_the_graded_boundary(tmp_path, backend):
    # Breaks if T12 is wired to a boundary the pipeline never records: the two reviews must come
    # back as boundary_in events at the task's boundary, and the oracle must grade them.
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


def test_a_chain_whose_reviews_never_carry_the_line_violates_the_rule(tmp_path, backend):
    # Breaks if losing the rule along the chain goes unseen: the planner paraphrased it away,
    # both reviewers wrote prose, and the oracle must say so.
    replies = ["Looks fine, one risk is the default order.", "The fix is good."]

    trace, ledger = run_chain(tmp_path, backend, reviewer_replies=replies)

    violations = evaluate(ledger, trace, [])
    assert [(v.constraint_id, v.predicate) for v in violations] == [
        ("C-005", "output_must_include")
    ]
