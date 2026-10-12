import pytest
from pydantic import ValidationError

from gate.trace import TraceWriter
from schemas.run import RunConfig
from schemas.trace import FinalOutput, ToolCall, parse_trace_jsonl


def config(tmp_path, **over):
    data = {
        "run_id": "r-7",
        "task_id": "X1",
        "arm": "mutated",
        "operator": "drop",
        "target_boundary": "delegation:lead->worker",
        "target_constraint": "C-001",
        "ledger_path": str(tmp_path / "l.yaml"),
        "trace_path": str(tmp_path / "run" / "trace.jsonl"),
        "seed": 1,
    }
    return RunConfig.model_validate({**data, **over})


def writer(tmp_path, **over):
    return TraceWriter(
        config(tmp_path, **over), model_id="m-1", safe_config_hash="sc", ledger_hash="lh"
    )


def test_emit_stamps_and_numbers_events_in_order(tmp_path):
    w = writer(tmp_path)
    w.emit(ToolCall, agent="worker", tool="ls", args={"path": "/"})
    w.emit(FinalOutput, agent="lead", content="done")
    events = parse_trace_jsonl((tmp_path / "run" / "trace.jsonl").read_text())
    assert [(e.step, e.type) for e in events] == [(0, "tool_call"), (1, "final_output")]
    stamps = {
        (e.run_id, e.arm, e.operator, e.model_id, e.safe_config_hash, e.ledger_hash) for e in events
    }
    assert stamps == {("r-7", "mutated", "drop", "m-1", "sc", "lh")}


def test_existing_trace_file_is_refused(tmp_path):
    path = tmp_path / "run" / "trace.jsonl"
    path.parent.mkdir()
    path.write_text("old\n")
    with pytest.raises(FileExistsError):
        writer(tmp_path)


def test_invalid_event_raises_and_writes_nothing(tmp_path):
    w = writer(tmp_path)
    with pytest.raises(ValidationError):
        w.emit(ToolCall, agent="", tool="ls", args={})
    assert (tmp_path / "run" / "trace.jsonl").read_text() == ""
