"""Helper-model extraction and compilation, with append-only ledger writes (SPEC C1)."""

import pytest
from langchain_core.messages import AIMessage

from common.llm import get_model
from ledger import load_ledger
from ledger.extract import (
    COMPILE_PROMPT,
    EXTRACT_PROMPT,
    Proposal,
    ProposedRule,
    append_constraints,
    compile_rule,
    extract_rules,
    main,
    next_ids,
    propose,
    to_constraint,
)
from ledger.recall import recall
from schemas.ledger import (
    Constraint,
    OutputMustInclude,
    PathForbidden,
    ToolForbidden,
    load_ledger_yaml,
)

AGENTS = ["lead", "worker"]
TOOLS = ["write_file", "fetch", "ask"]
LEDGER = """\
# a hand-written comment that must survive appends
version: 1
app:
  repo_root: /srv
constraints:
  - id: C-001
    text: "Keep out of vendor/"
    kind: constraint
    source: user
    authority: binding
    scope: [worker]
    origin: declared
    check: {type: path_forbidden, glob: "vendor/**", ops: [write]}
"""


def structured(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "id": "s1", "args": args}])


class Spy:
    """A stub helper that records the prompt behind each structured call."""

    def __init__(self, replies):
        self.model = get_model("helper", stub_responses=replies)
        self.prompts = []

    def with_structured_output(self, schema):
        bound = self.model.with_structured_output(schema)
        spy = self

        class _Recorder:
            def invoke(self, prompt):
                spy.prompts.append(prompt)
                return bound.invoke(prompt)

        return _Recorder()


RULE = {
    "text": "Never call fetch",
    "kind": "constraint",
    "source": "user",
    "authority": "binding",
    "scope": ["worker"],
}


def test_extract_rules_returns_the_helpers_proposals_and_names_the_agents():
    spy = Spy([structured("Proposals", {"rules": [RULE]})])
    rules = extract_rules(spy, "Do the export. Never call fetch.", AGENTS)
    assert [r.text for r in rules] == ["Never call fetch"]
    assert spy.prompts == [
        EXTRACT_PROMPT.format(agents="lead, worker", text="Do the export. Never call fetch.")
    ]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ({"type": "tool_forbidden", "name": "fetch"}, ToolForbidden(name="fetch")),
        (
            {"type": "path_forbidden", "glob": "vendor/**", "ops": ["write"], "name": None},
            PathForbidden(glob="vendor/**", ops=("write",)),
        ),
        ({"type": "none"}, None),
        ({"type": "path_forbidden", "ops": ["write"]}, None),  # no glob: not compilable
        ({"type": "output_must_include", "regex": "(unclosed", "agent": "worker"}, None),
        # A check naming a tool or agent the app does not have could never fire, so it must
        # not be recorded as verified.
        ({"type": "tool_forbidden", "name": "network"}, None),
        ({"type": "approval_before", "tool": "remove_file"}, None),
        ({"type": "output_must_include", "regex": "^OK", "agent": "auditor"}, None),
    ],
    ids=[
        "tool",
        "path",
        "none",
        "missing-field",
        "bad-regex",
        "unknown-tool",
        "unknown-approval-tool",
        "unknown-agent",
    ],
)
def test_compile_rule_returns_a_closed_list_check_or_none(reply, expected):
    spy = Spy([structured("CompiledCheck", reply)])
    assert compile_rule(spy, "a rule", AGENTS, TOOLS) == expected
    assert spy.prompts == [
        COMPILE_PROMPT.format(
            rule="a rule", agents="lead, worker", tools="write_file, fetch, ask", text="a rule"
        )
    ]


def test_prompts_name_no_task_or_repo():
    # Generality (AGENTS.md rule 5): agents and tools come in as arguments.
    for prompt in (EXTRACT_PROMPT, COMPILE_PROMPT):
        for word in (
            "migrations",
            "RISK",
            "planner",
            "coder",
            "reviewer",
            "http_get",
            "requirements",
            "dependenc",
        ):
            assert word not in prompt


def test_to_constraint_marks_it_extracted():
    c = to_constraint(ProposedRule(**RULE), ToolForbidden(name="fetch"), "C-002")
    assert (c.id, c.origin.value, c.check) == ("C-002", "extracted", ToolForbidden(name="fetch"))


def test_to_constraint_keeps_the_rule_unverified_when_its_check_is_inconsistent():
    # An output check naming an agent outside the rule's scope is refused by the schema; the
    # rule is kept, unverified, instead of being forced to fit.
    check = OutputMustInclude(regex="^OK", agent="lead")
    c = to_constraint(ProposedRule(**RULE), check, "C-002")
    assert c.check is None


def test_next_ids_continue_after_the_highest():
    ledger = load_ledger_text(LEDGER.replace("C-001", "C-007"))
    assert next_ids(ledger, 2) == ["C-008", "C-009"]


def load_ledger_text(text):
    return load_ledger_yaml(text)


def new_constraint(cid="C-002", **over):
    data = {
        **RULE,
        "id": cid,
        "origin": "extracted",
        "check": {"type": "tool_forbidden", "name": "fetch"},
    }
    return Constraint.model_validate({**data, **over})


def test_append_keeps_the_file_byte_for_byte_and_adds_the_entries(tmp_path):
    path = tmp_path / "ledger.yaml"
    path.write_text(LEDGER, encoding="utf-8")
    append_constraints(path, [new_constraint()])
    text = path.read_text(encoding="utf-8")
    assert text.startswith(LEDGER)  # append-only: nothing above is rewritten
    ledger = load_ledger(path).ledger
    assert [c.id for c in ledger.constraints] == ["C-001", "C-002"]
    assert ledger.get("C-002").check == ToolForbidden(name="fetch")
    assert ledger.app.repo_root == "/srv"


def test_append_refuses_an_id_collision_and_leaves_the_file_alone(tmp_path):
    path = tmp_path / "ledger.yaml"
    path.write_text(LEDGER, encoding="utf-8")
    with pytest.raises(ValueError, match="C-001"):
        append_constraints(path, [new_constraint("C-001")])
    assert path.read_text(encoding="utf-8") == LEDGER


def test_append_refuses_a_ledger_whose_constraints_are_not_last(tmp_path):
    # Appending text only works when the constraints list ends the file; anything else would
    # land the new entries under another key. The write is checked by reloading, and undone.
    reordered = LEDGER.replace("app:\n  repo_root: /srv\n", "") + "app:\n  repo_root: /srv\n"
    path = tmp_path / "ledger.yaml"
    path.write_text(reordered, encoding="utf-8")
    with pytest.raises(ValueError, match="last"):
        append_constraints(path, [new_constraint()])
    assert path.read_text(encoding="utf-8") == reordered


def test_propose_pairs_each_rule_with_its_compiled_check():
    spy = Spy(
        [
            structured("Proposals", {"rules": [RULE]}),
            structured("CompiledCheck", {"type": "tool_forbidden", "name": "fetch"}),
        ]
    )
    (proposal,) = propose(spy, "Never call fetch.", AGENTS, TOOLS)
    assert isinstance(proposal, Proposal)
    assert (proposal.rule.text, proposal.check) == ("Never call fetch", ToolForbidden(name="fetch"))


def test_propose_drops_agents_the_team_does_not_have():
    # A rule scoped to a made-up agent would never be re-pinned or checked anywhere.
    rule = {**RULE, "scope": ["worker", "developer"]}
    spy = Spy(
        [
            structured("Proposals", {"rules": [rule]}),
            structured("CompiledCheck", {"type": "tool_forbidden", "name": "fetch"}),
        ]
    )
    (proposal,) = propose(spy, "Never call fetch.", AGENTS, TOOLS)
    assert proposal.rule.scope == ["worker"]


def cli_model():
    return Spy(
        [
            structured("Proposals", {"rules": [RULE]}),
            structured("CompiledCheck", {"type": "tool_forbidden", "name": "fetch"}),
        ]
    )


def cli_args(tmp_path, *extra):
    ledger = tmp_path / "ledger.yaml"
    ledger.write_text(LEDGER, encoding="utf-8")
    text = tmp_path / "prompt.txt"
    text.write_text("Never call fetch.", encoding="utf-8")
    args = ["--ledger", str(ledger), "--text-file", str(text), "--agents", "lead,worker"]
    return ledger, [*args, "--tools", "write_file,fetch,ask", *extra]


def test_cli_dry_run_shows_proposals_and_writes_nothing(tmp_path, capsys):
    ledger, argv = cli_args(tmp_path, "--dry-run")
    assert main(argv, model=cli_model(), ask=lambda _: "y") == 0
    assert "Never call fetch" in capsys.readouterr().out
    assert ledger.read_text(encoding="utf-8") == LEDGER


@pytest.mark.parametrize(("answer", "added"), [("y", ["C-002"]), ("n", [])])
def test_cli_appends_only_what_the_human_confirms(tmp_path, answer, added):
    ledger, argv = cli_args(tmp_path)
    assert main(argv, model=cli_model(), ask=lambda _: answer) == 0
    ids = [c.id for c in load_ledger(ledger).ledger.constraints]
    assert ids == ["C-001", *added]
    if added:
        assert load_ledger(ledger).ledger.get("C-002").origin.value == "extracted"


def test_recall_counts_declared_checks_reproduced_exactly():
    cases = [
        ("text a", ToolForbidden(name="fetch")),
        ("text b", PathForbidden(glob="vendor/**", ops=("write",))),
    ]
    found = {
        "text a": [ToolForbidden(name="fetch")],
        "text b": [PathForbidden(glob="vendor/*", ops=("write",)), None],  # close is not a match
    }
    report = recall(cases, lambda text: found[text])
    assert (report.matched, report.total, report.rate) == (1, 2, 0.5)
    assert report.misses == ["text b"]
