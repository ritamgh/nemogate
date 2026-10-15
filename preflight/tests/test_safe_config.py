import hashlib
from pathlib import Path

import pytest

from preflight.probes import ProbeResult
from preflight.safe_config import derive, diff
from schemas.config import load_config
from schemas.enums import Role

CONFIG = load_config(Path(__file__).resolve().parents[2] / "config.yaml")
ROLES = (Role.helper, Role.sut, Role.planner)


def _result(probe, role, fmt, thinking, ok, tries=3):
    return ProbeResult(probe, role, fmt, thinking, tries, ok)


def _grid(**ok_by_check):
    """Every probe for every role, all passing 3/3, except the checks given as
    `<role>__<probe>_<format>_<thinking>=ok_count`, e.g. `sut__escape_json_object_off=0`."""
    results = []
    for role in ROLES:
        cells = {
            "escape_json_schema_off": ("escape", "json_schema", False),
            "escape_json_object_off": ("escape", "json_object", False),
            "tool_call_json_schema_off": ("tool_call", "json_schema", False),
            "thinking_json_schema_on": ("thinking", "json_schema", True),
            "model_ids_none_none": ("model_ids", None, None),
        }
        for name, (probe, fmt, thinking) in cells.items():
            tries = 1 if probe == "model_ids" else 3
            ok = ok_by_check.get(f"{role.value}__{name}", tries)
            results.append(_result(probe, role, fmt, thinking, ok, tries))
    return results


def test_super_failing_json_object_picks_json_schema_and_matches_the_repo_config():
    # The literal case from docs/FACTS.md section 6: Super 0/3 under json_object (prose), the rest
    # fine. Breaks if json_object is chosen, thinking is turned on, or the proposal's hash is not
    # the 8c5a0a1ec16e that config.yaml stores.
    proposal = derive(CONFIG, _grid(sut__escape_json_object_off=0))
    assert (proposal.response_format, proposal.thinking, proposal.hash) == (
        "json_schema",
        False,
        "8c5a0a1ec16e",
    )
    assert proposal.warnings == ()
    assert proposal.per_role["sut"]["json_object_thinking_off"] is False
    assert proposal.per_role["sut"]["json_schema_thinking_off"] is True
    assert diff(CONFIG, proposal) == []


def test_json_schema_is_preferred_when_both_formats_work():
    # Breaks if the second format wins a tie.
    assert derive(CONFIG, _grid()).response_format == "json_schema"


def test_json_object_is_chosen_when_json_schema_fails_for_any_role():
    # SPEC risk row: escape probe fails under json_schema -> json_object. One role failing is
    # enough, because the safe config has a single response_format for every role.
    proposal = derive(CONFIG, _grid(planner__escape_json_schema_off=2))
    assert proposal.response_format == "json_object"
    assert proposal.thinking is False
    # The hash text is the documented compact JSON of the settings and sampling (common.llm).
    text = (
        '{"response_format":"json_object","sampling":{"helper":{"temperature":0.0,"top_p":null},'
        '"planner":{"temperature":0.0,"top_p":null},"sut":{"temperature":1.0,"top_p":0.95}},'
        '"thinking":false}'
    )
    assert proposal.hash == hashlib.sha256(text.encode()).hexdigest()[:12]
    assert [name for name, _, _ in diff(CONFIG, proposal)] == ["response_format", "hash"]


def test_thinking_stays_off_even_when_it_works_on():
    # Breaks if "thinking works" turns it on: it costs 3-16x the output tokens (FACTS section 6).
    assert derive(CONFIG, _grid()).thinking is False


def test_thinking_on_is_proposed_only_when_no_format_works_with_it_off():
    # Breaks if the last-resort fallback is lost, or is silent.
    broken_off = {
        f"{role.value}__escape_{fmt}_off": 0
        for role in ROLES
        for fmt in ("json_schema", "json_object")
    }
    proposal = derive(CONFIG, _grid(**broken_off))
    assert (proposal.response_format, proposal.thinking) == ("json_schema", True)
    assert proposal.hash is not None
    assert "proposing thinking on" in proposal.warnings[0]


def test_no_working_setting_gives_no_hash_and_a_warning():
    # Breaks if a proposal (and a hash that get_model would accept) is made out of nothing.
    broken = {
        f"{role.value}__{name}": 0
        for role in ROLES
        for name in (
            "escape_json_schema_off",
            "escape_json_object_off",
            "thinking_json_schema_on",
        )
    }
    proposal = derive(CONFIG, _grid(**broken))
    assert (proposal.response_format, proposal.thinking, proposal.hash) == (None, None, None)
    assert proposal.warnings == (
        "no response format passed for every role: there is no safe config",
    )


def test_a_missing_probe_result_counts_as_not_passed():
    # Breaks if a role that was never probed is assumed fine.
    results = [r for r in _grid() if not (r.role == Role.sut and r.probe in ("escape", "thinking"))]
    assert derive(CONFIG, results).hash is None


def test_a_tool_call_written_as_text_and_an_unserved_id_become_warnings():
    # Breaks if these findings (which no config switch fixes) are dropped from the report.
    proposal = derive(
        CONFIG, _grid(sut__tool_call_json_schema_off=2, planner__model_ids_none_none=0)
    )
    assert proposal.response_format == "json_schema"
    assert proposal.warnings == (
        "sut: tool calls did not all come back native",
        "planner: the configured model id is not confirmed as served",
    )


def test_diff_lists_only_the_fields_that_differ():
    # Breaks if an unchanged field is reported, or a changed one missed.
    proposal = derive(
        CONFIG, _grid(sut__escape_json_schema_off=0, helper__thinking_json_schema_on=0)
    )
    assert proposal.response_format == "json_object"
    assert diff(CONFIG, proposal) == [
        ("response_format", "json_schema", "json_object"),
        ("hash", "8c5a0a1ec16e", proposal.hash),
    ]


@pytest.mark.parametrize("config_hash", [CONFIG.safe_config.hash])
def test_the_repo_config_is_the_baseline_these_tests_assume(config_hash):
    # Breaks (loudly) if config.yaml's safe config or sampling changes, so the literals above
    # are re-checked by a person rather than silently drifting.
    assert config_hash == "8c5a0a1ec16e"
