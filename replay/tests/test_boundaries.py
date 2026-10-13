"""replay/boundaries.py on its own: fork-point choice, the database copy, the recorder's hooks."""

import asyncio
import sqlite3
from types import SimpleNamespace
from uuid import UUID

import pytest

from replay.boundaries import BoundaryRecorder, Crossing, Recording, copy_checkpoints, fork_point


def crossing(call_id: str, sub: str, pre: str, image: str) -> Crossing:
    return Crossing(
        tool_call_id=call_id,
        delegation=f"delegation:planner->{sub}",
        returned=f"return:{sub}->planner",
        pre_checkpoint=pre,
        pre_image=image,
        post_checkpoint=f"{pre}-post",
        post_image=f"{image}-post",
    )


RECORDING = Recording(
    base_image="img-0",
    thread_id="r-1",
    crossings=(
        crossing("t1", "reviewer", "ck-1", "img-1"),
        crossing("t2", "coder", "ck-2", "img-2"),
        crossing("t3", "coder", "ck-3", "img-3"),
    ),
)


@pytest.mark.parametrize("boundary", ["delegation:planner->coder", "return:coder->planner"])
def test_fork_point_is_the_first_crossing_of_the_boundary_either_way(boundary):
    """Catches forking from a later crossing (earlier ones would stay untreated in the shared
    part) or, for a return, from after the reply (no root checkpoint lies between the subagent
    finishing and its reply: the call must run again)."""
    point = fork_point(RECORDING, boundary)
    assert (point.tool_call_id, point.pre_checkpoint, point.pre_image) == ("t2", "ck-2", "img-2")


def test_fork_point_of_a_boundary_the_run_never_crossed_is_none():
    assert fork_point(RECORDING, "delegation:planner->tester") is None


def test_copy_checkpoints_copies_every_row_and_leaves_the_source_alone(tmp_path):
    """Catches a copy that misses rows still in the write-ahead log of a WAL database."""
    src, dst = tmp_path / "a" / "checkpoints.sqlite", tmp_path / "b" / "checkpoints.sqlite"
    src.parent.mkdir()
    writer = sqlite3.connect(src)
    writer.execute("pragma journal_mode=wal")
    writer.execute("create table t (x)")
    writer.executemany("insert into t values (?)", [(1,), (2,)])
    writer.commit()  # still open: the rows sit in the -wal file

    copy_checkpoints(src, dst)
    writer.close()

    with sqlite3.connect(dst) as copy:
        assert copy.execute("select x from t order by x").fetchall() == [(1,), (2,)]


def test_copy_checkpoints_names_a_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError, match="nowhere.sqlite"):
        copy_checkpoints(tmp_path / "nowhere.sqlite", tmp_path / "copy.sqlite")
    assert not (tmp_path / "copy.sqlite").exists()


class Session:
    uuid = UUID(int=1)


def request(name: str, args: dict, call_id: str = "c1", agent: str | None = None):
    config = {
        "configurable": {"checkpoint_map": {"": "ck-9"}, "checkpoint_id": None},
        "metadata": {"lc_agent_name": agent},
    }
    tool_call = {"name": name, "args": args, "id": call_id, "type": "tool_call"}
    return SimpleNamespace(tool_call=tool_call, runtime=SimpleNamespace(config=config))


def advance(session: Session):
    def handler(req):
        session.uuid = UUID(int=2)
        return "reply"

    return handler


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_recorder_records_a_task_call_in_both_hooks(mode):
    """Catches the sync hook being missing or recording differently from the async one."""
    session = Session()
    recorder = BoundaryRecorder(session, "delegation:planner->coder")
    req = request("task", {"description": "d", "subagent_type": "coder"})
    handler = advance(session)
    if mode == "sync":
        assert recorder.wrap_tool_call(req, handler) == "reply"
    else:

        async def ahandler(r):
            return handler(r)

        assert asyncio.run(recorder.awrap_tool_call(req, ahandler)) == "reply"

    (got,) = recorder.crossings({"ck-9": "ck-10"})
    assert got == Crossing(
        tool_call_id="c1",
        delegation="delegation:planner->coder",
        returned="return:coder->planner",
        pre_checkpoint="ck-9",
        pre_image=str(UUID(int=1)),
        post_checkpoint="ck-10",
        post_image=str(UUID(int=2)),
    )


def test_recorder_names_the_sender_from_the_agent_name_when_there_is_one():
    session = Session()
    recorder = BoundaryRecorder(session, "delegation:planner->coder")
    req = request("task", {"description": "d", "subagent_type": "tester"}, agent="lead")
    recorder.wrap_tool_call(req, advance(session))
    (got,) = recorder.crossings({})
    assert (got.delegation, got.returned) == ("delegation:lead->tester", "return:tester->lead")
    assert got.post_checkpoint is None  # not in the history given


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("write_file", {"file_path": "/repo/a.py", "content": "x"}),
        ("task", {"description": "d", "subagent_type": "no such agent!"}),
        ("task", {"description": "d"}),
    ],
)
def test_recorder_ignores_calls_that_cross_no_boundary(name, args):
    """Catches recording ordinary tool calls, or a `task` call the gate does not treat as a
    delegation either (no valid subagent name), as boundary crossings."""
    session = Session()
    recorder = BoundaryRecorder(session, "delegation:planner->coder")
    assert recorder.wrap_tool_call(request(name, args), advance(session)) == "reply"
    assert recorder.crossings({}) == ()


def test_recorder_skips_a_call_from_an_agent_no_boundary_id_can_name():
    """Catches a recording that would crash the run: an agent name with a space cannot form a
    valid boundary id, so materialising the crossing would raise."""
    session = Session()
    recorder = BoundaryRecorder(session, "compaction:planner")
    req = request("task", {"description": "d", "subagent_type": "coder"}, agent="lead agent")
    assert recorder.wrap_tool_call(req, advance(session)) == "reply"
    assert recorder.crossings({}) == ()
