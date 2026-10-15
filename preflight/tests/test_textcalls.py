"""Tests for the offline text-tool-call scanner, on checkpoint files written by the real saver."""

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from operator import add
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from preflight.textcalls import find_runs, main, scan_all, scan_run

TEXT_CALL = (
    'I will write it now. <tool_call>{"name": "write_file", "arguments": '
    '{"file_path": "/repo/a.py", "content": "x = 1"}}</tool_call>'
)


class State(TypedDict):
    messages: Annotated[list, add]


def _node(message: AIMessage):
    return lambda state: {"messages": [message]}


def _run_graph(db: Path, root: list[AIMessage], sub: list[AIMessage] = ()) -> None:
    """A root graph that emits `root` in order; with `sub`, a subagent runs first."""
    parent = StateGraph(State)
    previous = START
    if sub:
        child = StateGraph(State)
        child_previous = START
        for i, message in enumerate(sub):
            child.add_node(f"c{i}", _node(message))
            child.add_edge(child_previous, f"c{i}")
            child_previous = f"c{i}"
        child.add_edge(child_previous, END)
        compiled = child.compile()

        def tools(state, config):  # like deepagents' `task` tool: runs the subagent inside the node
            compiled.invoke({"messages": []}, config)
            return {"messages": []}

        parent.add_node("tools", tools)
        parent.add_edge(START, "tools")
        previous = "tools"
    for i, message in enumerate(root):
        parent.add_node(f"p{i}", _node(message))
        parent.add_edge(previous, f"p{i}")
        previous = f"p{i}"
    parent.add_edge(previous, END)
    with closing(sqlite3.connect(db, check_same_thread=False)) as con:
        graph = parent.compile(checkpointer=SqliteSaver(con))
        graph.invoke({"messages": []}, {"configurable": {"thread_id": "r"}})


def _run_dir(base: Path, name: str, root, sub=(), trace=()) -> Path:
    run = base / name
    run.mkdir()
    _run_graph(run / "checkpoints.sqlite", root, sub)
    events = [
        {"type": kind, "agent": agent, "boundary": boundary, "step": i, "content": content}
        for i, (kind, agent, boundary, content) in enumerate(trace)
    ]
    (run / "trace.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    return run


def _ai(content, **kwargs):
    return AIMessage(content=content, id=f"m-{abs(hash(content + str(kwargs)))}", **kwargs)


PLANNER_EVENT = ("tool_call", "planner", None, "")


def test_a_tool_call_written_as_text_in_an_ai_message_is_found(tmp_path):
    # Breaks if the scanner skips root-thread messages or loses the tool name and excerpt.
    run = _run_dir(tmp_path, "r-1", [_ai(TEXT_CALL)], trace=[PLANNER_EVENT])
    scan = scan_run(run)
    assert scan.ai_messages == 1
    [hit] = scan.candidates
    assert (hit.run_id, hit.source, hit.agent, hit.where, hit.index) == (
        "r-1",
        "checkpoint",
        "planner",
        "root",
        0,
    )
    assert hit.tool == "write_file"
    assert hit.step == 0
    assert (
        hit.excerpt.startswith("ill write it now.") is False
    )  # centred on the call, not the start
    assert hit.excerpt.endswith("</tool_call>") and len(hit.excerpt) <= 200


def test_a_subagent_message_in_a_nested_namespace_is_found_with_its_agent_name(tmp_path):
    # Breaks if only the root namespace is read: the coder's messages live under `tools:...`.
    sub = [_ai(TEXT_CALL, name="coder"), _ai("done", name="coder")]
    run = _run_dir(tmp_path, "r-2", [_ai("all fixed")], sub=sub, trace=[PLANNER_EVENT])
    scan = scan_run(run)
    assert scan.ai_messages == 3
    assert dict(scan.agents) == {"coder": 2, "planner": 1}
    [hit] = scan.candidates
    assert hit.agent == "coder" and hit.where.startswith("tools:") and hit.index == 0


def test_a_native_tool_call_is_not_flagged_even_when_its_argument_looks_like_a_call(tmp_path):
    # Breaks if the scanner reads tool-call arguments: a write_file whose content is JSON is native.
    native = _ai(
        "",
        tool_calls=[
            {
                "name": "write_file",
                "args": {
                    "file_path": "/repo/spec.json",
                    "content": '{"name": "write_file", "arguments": {"file_path": "/x"}}',
                },
                "id": "call-1",
            }
        ],
    )
    run = _run_dir(tmp_path, "r-3", [native], trace=[PLANNER_EVENT])
    scan = scan_run(run)
    assert (scan.ai_messages, scan.candidates) == (1, [])


def test_prose_that_names_a_tool_is_not_flagged(tmp_path):
    # Breaks if a bare mention of a tool name counts as a call.
    prose = _ai("Next I will use write_file and run_tests, then call read_file on a.py.")
    run = _run_dir(tmp_path, "r-4", [prose], trace=[PLANNER_EVENT])
    assert scan_run(run).candidates == []


def test_scanning_leaves_the_checkpoint_file_and_its_folder_unchanged(tmp_path):
    # Breaks if the database is opened read-write (a journal or WAL file appears, bytes move).
    run = _run_dir(tmp_path, "r-5", [_ai(TEXT_CALL)], trace=[PLANNER_EVENT])
    db = run / "checkpoints.sqlite"
    before = (hashlib.sha256(db.read_bytes()).hexdigest(), sorted(p.name for p in run.iterdir()))
    scan_run(run)
    main([str(tmp_path)])
    assert (
        hashlib.sha256(db.read_bytes()).hexdigest(),
        sorted(p.name for p in run.iterdir()),
    ) == before


def test_a_fork_reports_only_its_own_messages_not_the_copied_source_history(tmp_path):
    # Breaks if a fork's copied prefix is counted again: the planted call would be reported twice.
    source = _run_dir(tmp_path, "r-src", [_ai(TEXT_CALL), _ai("ok")], trace=[PLANNER_EVENT])
    fork = tmp_path / "r-fork"
    fork.mkdir()
    shutil.copy(source / "checkpoints.sqlite", fork / "checkpoints.sqlite")
    own = _ai(TEXT_CALL.replace("write_file", "edit_file"))
    saver_config = {"configurable": {"thread_id": "r", "checkpoint_ns": "", "checkpoint_id": "zzz"}}
    with closing(sqlite3.connect(fork / "checkpoints.sqlite")) as con:
        SqliteSaver(con).put_writes(saver_config, [("messages", [own])], "task-fork")
    (fork / "fork.json").write_text(json.dumps({"source_run_id": "r-src"}), encoding="utf-8")

    by_run = {s.run_id: s for s in scan_all([tmp_path])}
    assert by_run["r-src"].ai_messages == 2 and len(by_run["r-src"].candidates) == 1
    assert by_run["r-fork"].prefix_separated is True
    assert by_run["r-fork"].ai_messages == 1
    assert [c.tool for c in by_run["r-fork"].candidates] == ["edit_file"]


def test_a_text_call_in_boundary_text_of_the_trace_is_found(tmp_path):
    # Breaks if trace.jsonl is ignored or the boundary text is not checked.
    trace = [
        ("boundary_in", "planner", "return:coder->planner", TEXT_CALL),
        ("final_output", "planner", None, '[TOOL_CALLS][{"name": "run_tests", "args": {}}]'),
        ("final_output", "planner", None, "Fixed the spelling everywhere."),
    ]
    run = _run_dir(tmp_path, "r-6", [_ai("done")], trace=trace)
    scan = scan_run(run)
    assert scan.trace_events == 3
    assert [(c.where, c.tool, c.index) for c in scan.candidates] == [
        ("boundary_in", "write_file", 0),
        ("final_output", "run_tests", 1),
    ]


def test_a_fork_scans_every_line_of_its_own_trace_and_never_prefix_jsonl(tmp_path):
    # Breaks if lines are skipped from a fork's trace.jsonl. The runner writes the copied prefix
    # to prefix.jsonl only; trace.jsonl holds just the fork's own events, numbered from step 0.
    run = _run_dir(
        tmp_path,
        "r-fork",
        [_ai("done")],
        trace=[("final_output", "planner", None, TEXT_CALL)],
    )
    (run / "fork.json").write_text(json.dumps({"source_run_id": "r-gone"}), encoding="utf-8")
    prefix = {"type": "final_output", "agent": "planner", "step": 0, "content": TEXT_CALL}
    (run / "prefix.jsonl").write_text(json.dumps(prefix) + "\n", encoding="utf-8")
    scan = scan_run(run)
    assert scan.prefix_separated is False  # source folder not found: stated, not hidden
    assert scan.trace_events == 1
    assert [(c.where, c.tool, c.index) for c in scan.candidates] == [
        ("final_output", "write_file", 0)
    ]


def test_sealed_run_folders_are_never_scanned(tmp_path):
    # Breaks if a folder named *heldout* is opened (AGENTS.md rule 6).
    _run_dir(tmp_path, "r-7", [_ai("ok")])
    sealed = tmp_path / "heldout-t7"
    sealed.mkdir()
    _run_dir(sealed, "r-8", [_ai("ok")])
    assert [p.name for p in find_runs([tmp_path])] == ["r-7"]


def test_the_summary_line_and_the_json_output(tmp_path, capsys):
    # Breaks if the summary format changes or --json drops a candidate.
    _run_dir(tmp_path, "r-9", [_ai(TEXT_CALL), _ai("fine")], trace=[PLANNER_EVENT])
    _run_dir(tmp_path, "r-10", [_ai("fine")], trace=[PLANNER_EVENT])
    scanned = tmp_path / "runs"
    scanned.mkdir()
    for run in list(tmp_path.glob("r-*")):
        run.rename(scanned / run.name)
    out = tmp_path / "out" / "result.json"
    out.parent.mkdir()
    assert main([str(scanned), "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "runs scanned 2, AI messages 3, candidates 1 (runs with any: 1)" in printed
    assert "r-9  checkpoint  agent=planner  root  step=0  #0  looks like: write_file" in printed
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert [r["run_id"] for r in saved] == ["r-10", "r-9"]
    assert saved[1]["candidates"][0]["tool"] == "write_file"


def test_json_output_is_refused_where_it_would_overwrite_or_sit_in_a_run(tmp_path):
    # Breaks if --json can replace a run's trace.jsonl (a trace-only run has no checkpoint file),
    # write into a subfolder of a run, replace any existing file, or land in a scanned folder.
    trace_only = tmp_path / "runs" / "r-t"
    trace_only.mkdir(parents=True)
    (trace_only / "trace.jsonl").write_text("", encoding="utf-8")
    with_checkpoints = _run_dir(tmp_path / "runs", "r-c", [_ai("ok")])
    (with_checkpoints / "sub").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.json").write_text("precious", encoding="utf-8")
    for target in (
        trace_only / "trace.jsonl",
        with_checkpoints / "sub" / "out.json",
        elsewhere / "keep.json",
        tmp_path / "runs" / "result.json",
    ):
        with pytest.raises(SystemExit):
            main([str(tmp_path / "runs"), "--json", str(target)])
    assert (trace_only / "trace.jsonl").read_text(encoding="utf-8") == ""
    assert (elsewhere / "keep.json").read_text(encoding="utf-8") == "precious"
    assert not (with_checkpoints / "sub" / "out.json").exists()
    assert not (tmp_path / "runs" / "result.json").exists()
