import pytest
from pydantic import TypeAdapter, ValidationError

from schemas.enums import (
    AgentName,
    Arm,
    Authority,
    BoundaryId,
    ConstraintId,
    ConstraintKind,
    EventType,
    FileOp,
    FsOp,
    Operator,
    Origin,
    Role,
    Source,
)


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        (Arm, ["natural", "mutated", "block_only", "gated"]),
        (
            Operator,
            ["none", "drop", "hedge", "compress_100", "compress_25", "return_corrupt"],
        ),
        (Role, ["helper", "sut", "planner"]),
        (FsOp, ["added", "modified", "deleted"]),
        (FileOp, ["write", "delete"]),
        (ConstraintKind, ["constraint", "scope", "fact"]),
        (Source, ["user", "system", "tool", "agent_inference"]),
        (Authority, ["binding", "advisory"]),
        (Origin, ["declared", "extracted"]),
        (
            EventType,
            [
                "boundary_out",
                "boundary_in",
                "tool_call",
                "tool_result",
                "approval",
                "gate_block",
                "final_output",
            ],
        ),
    ],
)
def test_enum_values_match_the_spec_spelling(enum, values):
    # Catches a renamed or dropped member: both owners write these strings to disk.
    assert [member.value for member in enum] == values


def test_enum_str_is_the_plain_value():
    # Catches a plain Enum: str() would give "Arm.natural" and break JSONL and CLI output.
    assert str(Arm.block_only) == "block_only"


CONSTRAINT_ID = TypeAdapter(ConstraintId)
BOUNDARY_ID = TypeAdapter(BoundaryId)
AGENT_NAME = TypeAdapter(AgentName)


@pytest.mark.parametrize("value", ["C-001", "C-042", "C-1000"])
def test_constraint_id_accepts_three_or_more_digits(value):
    assert CONSTRAINT_ID.validate_python(value) == value


@pytest.mark.parametrize("value", ["C1", "C-01", "c-001", "C-001x", "C-001\n", " C-001", ""])
def test_constraint_id_rejects_malformed(value):
    # Catches a loose pattern (e.g. unanchored or \d*) that lets bad ids into the ledger.
    with pytest.raises(ValidationError):
        CONSTRAINT_ID.validate_python(value)


@pytest.mark.parametrize(
    "value",
    [
        "delegation:planner->coder",
        "return:coder->planner",
        "compaction:planner",
        "delegation:agent_1->sub-agent",
    ],
)
def test_boundary_id_accepts_the_three_forms(value):
    assert BOUNDARY_ID.validate_python(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "delegation:planner",
        "compaction:a->b",
        "handoff:a->b",
        "delegation:a->",
        "delegation:->b",
        "delegation:a->b->c",
        "delegation:a b->c",
        "",
    ],
)
def test_boundary_id_rejects_malformed(value):
    with pytest.raises(ValidationError):
        BOUNDARY_ID.validate_python(value)


@pytest.mark.parametrize("value", ["coder", "sub-agent_2"])
def test_agent_name_accepts_identifiers(value):
    assert AGENT_NAME.validate_python(value) == value


@pytest.mark.parametrize("value", ["", "two words", "a/b", "a->b"])
def test_agent_name_rejects_non_identifiers(value):
    with pytest.raises(ValidationError):
        AGENT_NAME.validate_python(value)
