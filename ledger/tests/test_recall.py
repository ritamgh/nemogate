"""The recall CLI, the development set, and the match-preserving canonical form of a check."""

import json
import re
from pathlib import Path

import pytest

from ledger import load_ledger
from ledger.extract import _canonical, compile_rule, propose
from ledger.recall import load_cases, main
from ledger.tests.test_extract import AGENTS, TOOLS, Spy, structured
from oracle.paths import glob_matches
from schemas.ledger import OutputMustInclude, ToolForbidden

DEV = Path(__file__).parent / "fixtures" / "recall_dev.yaml"
ROOT = Path(__file__).resolve().parents[2]
TEXTS = [
    "",
    "\n",
    "VERDICT: ok",
    "VERDICT:no",
    "intro\nVERDICT: yes\nend",
    "x VERDICT: inline",
    "a",
    "a..",
    "aaa",
    "X",
    "line\nX last",
    "xzy",
]
PATHS = ["assets/a.png", "assets/x/y.png", "assets", "other/a.png", "assets.png"]


@pytest.mark.parametrize(
    ("regex", "canonical"),
    [
        ("(?m)^VERDICT: .*", "(?m)^VERDICT: "),
        ("(?m)^VERDICT: .*$", "(?m)^VERDICT: "),
        ("a.*.*", "a"),
        ("^X.*$", "^X.*$"),  # not multiline: `$` only at the end, so `.*$` is not free
        (r"a\.*", r"a\.*"),  # a literal dot repeated, not "anything"
        ("x.*y", "x.*y"),
        (".*", ".*"),  # never rewritten to an empty regex
        ("Brightline", "Brightline"),
    ],
)
def test_canonical_regex_matches_exactly_what_the_original_matches(regex, canonical):
    out = _canonical({"regex": regex})["regex"]
    assert out == canonical
    for text in TEXTS:
        assert bool(re.search(out, text)) == bool(re.search(regex, text)), text


@pytest.mark.parametrize(
    ("glob", "canonical"),
    [("assets/*", "assets/**"), ("assets/**", "assets/**"), ("a.txt", "a.txt")],
)
def test_canonical_glob_matches_exactly_what_the_original_matches(glob, canonical):
    assert _canonical({"glob": glob}) == {"glob": canonical}
    assert _canonical({"globs": [glob]}) == {"globs": [canonical]}
    for path in PATHS:
        assert glob_matches(canonical, path) == glob_matches(glob, path), path


def test_compile_rule_canonicalises_the_helpers_check():
    reply = {"type": "output_must_include", "regex": "(?m)^OK: .*", "agent": "worker"}
    spy = Spy([structured("CompiledCheck", reply)])
    assert compile_rule(spy, "a rule", AGENTS, TOOLS) == OutputMustInclude(
        regex="(?m)^OK: ", agent="worker"
    )


def test_compile_sees_the_text_the_rule_came_from():
    # The rule alone may not name the file or tool it means ("no new packages"); the text does.
    rule = {"text": "Never call fetch.", "kind": "constraint", "source": "user",
            "authority": "binding", "scope": ["worker"]}  # fmt: skip
    spy = Spy(
        [
            structured("Proposals", {"rules": [rule]}),
            structured("CompiledCheck", {"type": "tool_forbidden", "name": "fetch"}),
        ]
    )
    text = "Do the export. Never call fetch."
    (proposal,) = propose(spy, text, AGENTS, TOOLS)
    assert proposal.check == ToolForbidden(name="fetch")
    assert spy.prompts[1].endswith(f"Rule:\nNever call fetch.\n\nText:\n{text}")


def test_the_development_set_is_valid_and_shares_no_check_with_the_reference_ledger():
    agents, tools, cases = load_cases(DEV)
    assert len(cases) >= 10 and any(c.expect is None for c in cases)
    names = {c.expect.model_dump().get(k) for c in cases if c.expect for k in ("name", "tool")}
    assert names - {None} <= set(tools)
    # What this checks, no more: other agent names, and no expected check equal to a check in
    # the reference ledger. The dev cases do share the reference rules' categories and some
    # shapes (FACTS §6), so the reference tasks are not a clean held-out set for these prompts.
    assert not set(agents) & {"planner", "coder", "reviewer"}
    reference = {c.check for c in load_ledger(ROOT / "nemogate.yaml").ledger.constraints}
    assert not {c.expect for c in cases if c.expect} & reference


def test_recall_cli_counts_hits_and_extras(tmp_path, capsys):
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        "agents: [lead, worker]\ntools: [fetch]\ncases:\n"
        "  - {name: one, text: Never call fetch., expect: {type: tool_forbidden, name: fetch}}\n"
        "  - {name: none, text: Just do it., expect: null}\n"
    )
    rule = {"text": "Never call fetch.", "kind": "constraint", "source": "user",
            "authority": "binding", "scope": ["worker"]}  # fmt: skip
    model = Spy(
        [
            structured("Proposals", {"rules": [rule]}),
            structured("CompiledCheck", {"type": "tool_forbidden", "name": "fetch"}),
            structured("Proposals", {"rules": [rule]}),  # a rule invented for the second text
            structured("CompiledCheck", {"type": "tool_forbidden", "name": "fetch"}),
        ]
    )
    out = tmp_path / "out.json"
    assert main(["--cases", str(cases), "--out", str(out)], model=model) == 0
    assert "recall 1/1; extra checks 1 over 2 texts" in capsys.readouterr().out
    result = json.loads(out.read_text())
    assert result["matched"] == 1
    assert result["rows"][0]["matched_rules"] == ["Never call fetch."]
    assert result["rows"][1]["matched_rules"] == []


def test_recall_cli_refuses_without_live(monkeypatch, capsys):
    monkeypatch.delenv("NEMOGATE_LIVE", raising=False)
    assert main(["--cases", str(DEV)]) == 2
    assert "NEMOGATE_LIVE" in capsys.readouterr().err


def test_every_rule_that_compiled_to_the_hit_is_recorded(tmp_path):
    # A first-match record once made a hit look as if only a stray rule produced it, when the
    # rule stating it had compiled to the same check (FACTS §6).
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        "agents: [lead, worker]\ntools: [fetch]\ncases:\n"
        "  - {name: one, text: Use RFC 1. Never call fetch.,\n"
        "     expect: {type: tool_forbidden, name: fetch}}\n"
    )
    stray = {"text": "Use RFC 1.", "kind": "constraint", "source": "user",
             "authority": "binding", "scope": ["worker"]}  # fmt: skip
    real = {**stray, "text": "Never call fetch."}
    check = structured("CompiledCheck", {"type": "tool_forbidden", "name": "fetch"})
    model = Spy([structured("Proposals", {"rules": [stray, real]}), check, check])
    out = tmp_path / "out.json"
    assert main(["--cases", str(cases), "--out", str(out)], model=model) == 0
    assert json.loads(out.read_text())["rows"][0]["matched_rules"] == [
        "Use RFC 1.",
        "Never call fetch.",
    ]
