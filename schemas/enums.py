"""Shared names for NemoGate: enums, strict model config and constrained id types.

Spellings here are the contract between A's code and B's code (docs/SPEC.md, "Shared names").
"""

from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, StringConstraints

# Every schema model uses this config: unknown keys are rejected, instances are immutable.
STRICT = ConfigDict(extra="forbid", frozen=True)


class Arm(StrEnum):
    natural = "natural"
    mutated = "mutated"
    block_only = "block_only"
    gated = "gated"


class Operator(StrEnum):
    none = "none"
    drop = "drop"
    hedge = "hedge"
    compress_100 = "compress_100"
    compress_25 = "compress_25"
    return_corrupt = "return_corrupt"


class Role(StrEnum):
    helper = "helper"
    sut = "sut"
    planner = "planner"


class FsOp(StrEnum):
    added = "added"
    modified = "modified"
    deleted = "deleted"


class FileOp(StrEnum):
    """Operations a path_forbidden predicate can forbid."""

    write = "write"
    delete = "delete"


class ConstraintKind(StrEnum):
    constraint = "constraint"
    scope = "scope"
    fact = "fact"


class Source(StrEnum):
    user = "user"
    system = "system"
    tool = "tool"
    agent_inference = "agent_inference"


class Authority(StrEnum):
    binding = "binding"
    advisory = "advisory"


class Origin(StrEnum):
    declared = "declared"
    extracted = "extracted"


class EventType(StrEnum):
    boundary_out = "boundary_out"
    boundary_in = "boundary_in"
    tool_call = "tool_call"
    tool_result = "tool_result"
    approval = "approval"
    gate_block = "gate_block"
    final_output = "final_output"


ConstraintId = Annotated[str, StringConstraints(pattern=r"^C-\d{3,}$")]
# Agent names are free-form identifiers, never an enum of names.
BoundaryId = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^(?:(?:delegation|return):[A-Za-z0-9_-]+->[A-Za-z0-9_-]+|compaction:[A-Za-z0-9_-]+)$"
        )
    ),
]
AgentName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]+$")]
