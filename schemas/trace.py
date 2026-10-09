"""Event trace: one JSON object per line (docs/SPEC.md, "Event trace")."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from schemas.enums import STRICT, AgentName, Arm, BoundaryId, ConstraintId, Operator


class _EventBase(BaseModel):
    """Fields on every event. Not part of the `Event` union."""

    model_config = STRICT
    run_id: str
    arm: Arm
    operator: Operator
    boundary: BoundaryId | None = None
    step: int = Field(ge=0)
    agent: AgentName
    model_id: str
    safe_config_hash: str
    ledger_hash: str


class BoundaryOut(_EventBase):
    """The handoff payload as sent."""

    type: Literal["boundary_out"] = "boundary_out"
    boundary: BoundaryId
    content: str


class BoundaryIn(_EventBase):
    """The handoff payload as received."""

    type: Literal["boundary_in"] = "boundary_in"
    boundary: BoundaryId
    content: str


class ToolCall(_EventBase):
    type: Literal["tool_call"] = "tool_call"
    tool: str
    args: dict[str, Any]
    blocked: bool = False


class ToolResult(_EventBase):
    type: Literal["tool_result"] = "tool_result"
    tool: str
    content: str
    is_error: bool = False


class Approval(_EventBase):
    type: Literal["approval"] = "approval"
    action: str
    reason: str | None = None
    approved: bool


class GateBlock(_EventBase):
    type: Literal["gate_block"] = "gate_block"
    tool: str | None = None
    constraint_id: ConstraintId
    reason: str


class FinalOutput(_EventBase):
    type: Literal["final_output"] = "final_output"
    content: str


Event = Annotated[
    BoundaryOut | BoundaryIn | ToolCall | ToolResult | Approval | GateBlock | FinalOutput,
    Field(discriminator="type"),
]
EVENT = TypeAdapter(Event)
EVENT_LIST = TypeAdapter(list[Event])


def parse_trace_jsonl(text: str) -> list[Event]:
    """Parse a JSONL trace, skipping blank lines. A bad line raises ValueError naming "line N"."""
    events: list[Event] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(EVENT.validate_json(line))
        except ValidationError as exc:
            raise ValueError(f"line {number}: {exc}") from exc
    return events


def dump_event_jsonl(event: Event) -> str:
    """One JSON line, no trailing newline."""
    return event.model_dump_json()
