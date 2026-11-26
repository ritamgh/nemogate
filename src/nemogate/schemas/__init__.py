"""Shared formats between A's code and B's code. See docs/SPEC.md, "Shared interfaces"."""

from nemogate.schemas.config import AppConfig, load_config
from nemogate.schemas.contracts import EvaluateFn, MakeMiddlewareFn, RunFn
from nemogate.schemas.run import FS_DIFF, FsChange, RunConfig, RunRecord, Violation, is_comparable
from nemogate.schemas.trace import (
    EVENT,
    EVENT_LIST,
    Approval,
    BoundaryIn,
    BoundaryOut,
    Event,
    FinalOutput,
    GateBlock,
    ToolCall,
    ToolResult,
    dump_event_jsonl,
    parse_trace_jsonl,
)

__all__ = [
    "EVENT",
    "EVENT_LIST",
    "FS_DIFF",
    "AppConfig",
    "Approval",
    "BoundaryIn",
    "BoundaryOut",
    "EvaluateFn",
    "Event",
    "FinalOutput",
    "FsChange",
    "GateBlock",
    "MakeMiddlewareFn",
    "RunConfig",
    "RunFn",
    "RunRecord",
    "ToolCall",
    "ToolResult",
    "Violation",
    "dump_event_jsonl",
    "is_comparable",
    "load_config",
    "parse_trace_jsonl",
]
