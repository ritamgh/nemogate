import json

import pytest
from pydantic import ValidationError

from schemas.enums import Arm, Operator
from schemas.trace import (
    EVENT,
    EVENT_LIST,
    Approval,
    BoundaryIn,
    BoundaryOut,
    FinalOutput,
    GateBlock,
    ToolCall,
    ToolResult,
    dump_event_jsonl,
    parse_trace_jsonl,
)

# Verbatim from docs/SPEC.md, "Event trace".
SPEC_TRACE_LINE = (
    '{"run_id":"r-0042","arm":"mutated","operator":"hedge",'
    '"boundary":"delegation:planner->coder","step":12,"agent":"coder","type":"tool_call",'
    '"tool":"write_file","args":{"path":"migrations/0003.py"},"blocked":false,'
    '"model_id":"nvidia/nemotron-3-super-120b-a12b","safe_config_hash":"9c1f2a",'
    '"ledger_hash":"41ab07"}'
)

COMMON = {
    "run_id": "r-0042",
    "arm": "mutated",
    "operator": "hedge",
    "step": 3,
    "agent": "coder",
    "model_id": "nvidia/nemotron-3-super-120b-a12b",
    "safe_config_hash": "9c1f2a",
    "ledger_hash": "41ab07",
}

# One valid example per event type, with the model each must parse to.
EXAMPLES = [
    (
        BoundaryOut,
        {
            **COMMON,
            "type": "boundary_out",
            "boundary": "delegation:planner->coder",
            "content": "Do not touch migrations/.",
        },
    ),
    (
        BoundaryIn,
        {
            **COMMON,
            "type": "boundary_in",
            "boundary": "return:coder->planner",
            "content": "Done.",
        },
    ),
    (
        ToolCall,
        {**COMMON, "type": "tool_call", "tool": "run_tests", "args": {"k": 1}, "blocked": True},
    ),
    (
        ToolResult,
        {**COMMON, "type": "tool_result", "tool": "run_tests", "content": "ok", "is_error": False},
    ),
    (
        Approval,
        {**COMMON, "type": "approval", "action": "delete_file", "reason": None, "approved": True},
    ),
    (
        GateBlock,
        {
            **COMMON,
            "type": "gate_block",
            "tool": "write_file",
            "constraint_id": "C-001",
            "reason": "path matches a forbidden glob",
        },
    ),
    (FinalOutput, {**COMMON, "type": "final_output", "content": "All done."}),
]
EXAMPLE_IDS = [model.__name__ for model, _ in EXAMPLES]


def test_spec_trace_line_parses_to_tool_call():
    # Breaks if the schema drifts from the line B's middleware is specified to write.
    event = EVENT.validate_json(SPEC_TRACE_LINE)
    assert isinstance(event, ToolCall)
    assert event.tool == "write_file"
    assert event.blocked is False
    assert event.args == {"path": "migrations/0003.py"}
    assert event.boundary == "delegation:planner->coder"
    assert event.arm is Arm.mutated
    assert event.operator is Operator.hedge


@pytest.mark.parametrize(("model", "data"), EXAMPLES, ids=EXAMPLE_IDS)
def test_each_event_type_parses_to_its_model(model, data):
    # Breaks if a discriminator value is mapped to the wrong model.
    assert type(EVENT.validate_python(data)) is model


def test_unknown_event_type_is_rejected():
    # Breaks if the union silently accepts event types outside the closed list.
    with pytest.raises(ValidationError):
        EVENT.validate_python({**COMMON, "type": "thought", "content": "hmm"})


def test_tool_call_without_tool_is_rejected():
    with pytest.raises(ValidationError):
        EVENT.validate_python({**COMMON, "type": "tool_call", "args": {}})


def test_extra_key_on_event_is_rejected():
    # Breaks if extra="forbid" is lost, which would hide a misspelled field.
    with pytest.raises(ValidationError):
        EVENT.validate_python({**COMMON, "type": "final_output", "content": "x", "blokced": 1})


def test_boundary_out_requires_a_boundary():
    # Breaks if BoundaryOut inherits the optional boundary of the base class.
    with pytest.raises(ValidationError):
        EVENT.validate_python({**COMMON, "type": "boundary_out", "boundary": None, "content": "x"})


def test_boundary_in_requires_a_boundary():
    with pytest.raises(ValidationError):
        EVENT.validate_python({**COMMON, "type": "boundary_in", "content": "x"})


def test_boundary_without_kind_prefix_is_rejected():
    with pytest.raises(ValidationError):
        EVENT.validate_python(
            {**COMMON, "type": "boundary_out", "boundary": "planner->coder", "content": "x"}
        )


def test_boundary_with_empty_agent_is_rejected():
    with pytest.raises(ValidationError):
        EVENT.validate_python(
            {**COMMON, "type": "boundary_out", "boundary": "delegation:a->", "content": "x"}
        )


@pytest.mark.parametrize("field", ["run_id", "model_id", "safe_config_hash", "ledger_hash"])
@pytest.mark.parametrize("blank", ["", " ", "\n"], ids=["empty", "space", "newline"])
def test_blank_fingerprint_on_event_is_rejected(field, blank):
    # Breaks if the gate can stamp an unset fingerprint onto a trace line.
    data = {**COMMON, "type": "final_output", "content": "ok", field: blank}
    with pytest.raises(ValidationError):
        EVENT.validate_python(data)


def test_negative_step_is_rejected():
    with pytest.raises(ValidationError):
        EVENT.validate_python({**COMMON, "step": -1, "type": "final_output", "content": "x"})


def test_parse_trace_jsonl_skips_blank_lines():
    # Breaks if blank lines (e.g. a trailing newline pair) are treated as events or errors.
    final = dump_event_jsonl(EVENT.validate_python(EXAMPLES[-1][1]))
    text = f"{SPEC_TRACE_LINE}\n\n{final}\n  \n{SPEC_TRACE_LINE}\n"
    events = parse_trace_jsonl(text)
    assert [type(e) for e in events] == [ToolCall, FinalOutput, ToolCall]


def test_parse_trace_jsonl_names_the_bad_line():
    # Breaks if the error does not say which line of a long trace is bad.
    final = dump_event_jsonl(EVENT.validate_python(EXAMPLES[-1][1]))
    text = f"{final}\n{json.dumps({**COMMON, 'type': 'nope'})}\n{final}\n"
    with pytest.raises(ValueError, match="line 2"):
        parse_trace_jsonl(text)


def test_parse_trace_jsonl_counts_blank_lines_in_line_numbers():
    # Breaks if line numbers are counted after blank lines are dropped.
    final = dump_event_jsonl(EVENT.validate_python(EXAMPLES[-1][1]))
    with pytest.raises(ValueError, match="line 3"):
        parse_trace_jsonl(f"{final}\n\nnot json\n")


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_unicode_line_separators_inside_a_json_string_round_trip(separator):
    # Breaks if the trace is split with str.splitlines(): it also splits on U+2028,
    # U+2029, U+0085 and others, which model output can legitimately contain.
    event = FinalOutput(**COMMON, content=f"first{separator}second")
    parsed = parse_trace_jsonl(dump_event_jsonl(event) + "\n")
    assert parsed == [event]
    assert parsed[0].content == f"first{separator}second"


def test_parse_trace_jsonl_accepts_crlf_line_endings():
    # Breaks if a trailing "\r" is left on the line (or CRLF files are rejected).
    final = dump_event_jsonl(EVENT.validate_python(EXAMPLES[-1][1]))
    assert len(parse_trace_jsonl(f"{final}\r\n{final}\r\n")) == 2


@pytest.mark.parametrize(("model", "data"), EXAMPLES, ids=EXAMPLE_IDS)
def test_dump_and_validate_round_trip(model, data):
    # Breaks if a field is lost or renamed between dump_event_jsonl and EVENT.validate_json.
    event = EVENT.validate_python(data)
    line = dump_event_jsonl(event)
    assert EVENT.validate_json(line) == event
    assert '"arm":"mutated"' in line
    assert "\n" not in line
    assert EVENT_LIST.validate_python([event]) == [event]
