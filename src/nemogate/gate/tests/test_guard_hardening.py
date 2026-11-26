"""Hardening found by the security audit: an approval tool's plain-text "no" is not an approval,
the per-thread counters and approvals are bounded in memory, and a very long path cannot make the
glob walk blow up (F1, F2, F3)."""

import time

import pytest
from langchain_core.messages import ToolMessage

from nemogate.gate.errors import GuardLoopError
from nemogate.gate.middleware import approval_denied, glob_may_match_below
from nemogate.gate.state import MAX_BLOCKS_PER_THREAD, MAX_THREADS, GuardState
from nemogate.gate.tests.test_guard_wiring import (
    Tool,
    approval_run,
    make,
    message,
    req,
    run,
    thread,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """The guard resolves paths against the cwd; keep it an empty folder."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


DROP = "Blocked by rule ask-before-delete: Ask a human before deleting."

# F1. A result that reads as a denial is never recorded as an approval

DENIALS = [
    "Denied by user",
    "No",
    "no thanks",
    "Rejected: not allowed",
    "not approved",
    "Not approved by the admin.",
    "  declined",
    "CANCELLED",
    "n",
    '"No"',
    '{"decision": "denied"}',
    '{"decision": "Rejected"}',
    '{"decision": false}',
]
APPROVALS = [
    "yes",
    "Approved",
    "ok, go ahead",
    "Notice: approved by admin",
    "Note: nothing to decide",
    "Nobody objected",
    "",
    '{"approved": true}',
    '{"decision": "approved"}',
]


@pytest.mark.parametrize("text", DENIALS)
def test_a_plain_text_denial_is_not_recorded_as_an_approval(tmp_path, repo, text):
    # Breaks if any non-error result of the approval tool counts: "Denied by user" would unlock
    # drop_table.
    result, drop = approval_run(tmp_path, message(text))
    assert drop.ran == []
    assert result.content == DROP


@pytest.mark.parametrize("text", APPROVALS)
def test_any_other_reply_still_counts_as_an_approval(tmp_path, repo, text):
    # Breaks if the classifier is widened past the denial words: "Notice: ..." starts with "no"
    # but its first word is not one.
    result, drop = approval_run(tmp_path, message(text))
    assert drop.ran == [{"name": "users"}]


def test_a_bare_dict_denial_from_the_tool_is_not_an_approval(tmp_path, repo):
    result, drop = approval_run(tmp_path, lambda r: {"approved": False})
    assert drop.ran == []
    assert result.content == DROP


def test_a_bare_text_denial_from_the_tool_is_not_an_approval(tmp_path, repo):
    # Breaks if only a ToolMessage's text is read: some tools return a plain string.
    result, drop = approval_run(tmp_path, lambda r: "Denied by user")
    assert drop.ran == []
    assert result.content == DROP


def test_a_denial_does_not_revoke_an_earlier_approval(tmp_path, repo):
    # FR-019 stays: one approval in a thread unlocks later calls; a later "No" does not revoke it.
    _, mw = make(tmp_path)
    drop = Tool()
    with thread():
        mw.wrap_tool_call(req("ask_human", {}), message("yes"))
        mw.wrap_tool_call(req("ask_human", {}), message("No"))
        run(mw, drop, "drop_table", {"name": "users"})
    assert drop.ran == [{"name": "users"}]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("Denied by user", True),
        ("No", True),
        ("no thanks", True),
        ("Rejected: not allowed", True),
        ("not approved", True),
        ("Stop.", True),
        ("yes", False),
        ("Approved", False),
        ("ok, go ahead", False),
        ("Notice: approved by admin", False),
        ("", False),
        ({"approved": False}, True),
        ({"approved": True}, False),
        ({"decision": "no"}, True),
        ({"decision": "yes"}, False),
        (ToolMessage(content="No", tool_call_id="c", name="t"), True),
        (ToolMessage(content="yes", tool_call_id="c", name="t"), False),
        (ToolMessage(content="boom", tool_call_id="c", name="t", status="error"), True),
        (ToolMessage(content=[{"type": "text", "text": "Declined"}], tool_call_id="c"), True),
    ],
)
def test_approval_denied_classifies_literal_answers(result, expected):
    assert approval_denied(result) is expected


# F2. Counters and approvals are bounded in memory


def test_300_distinct_blocked_calls_keep_at_most_256_counters_and_a_repeat_still_trips(
    tmp_path, repo
):
    # Breaks if the per-thread counter map grows without bound, or if the cap drops the counter of
    # a call that is still being repeated.
    guard, mw = make(tmp_path)
    ran = Tool()
    with thread("t1"):
        for i in range(300):
            run(mw, ran, "run_shell", {"cmd": f"ls {i}"}, f"c{i}")
        run(mw, ran, "run_shell", {"cmd": "same"}, "x1")
        run(mw, ran, "run_shell", {"cmd": "same"}, "x2")
        with pytest.raises(GuardLoopError):
            run(mw, ran, "run_shell", {"cmd": "same"}, "x3")
    assert len(guard.state._threads["t1"].blocks) <= MAX_BLOCKS_PER_THREAD == 256
    assert ran.ran == []


def test_a_call_repeated_among_other_blocks_keeps_its_count():
    # Breaks if the oldest counter is dropped by insertion time only: a looping call that is
    # blocked again and again must not be forgotten because 256 other calls came in between.
    state = GuardState()
    assert state.record_block("t", "r", "tool", {"a": 0}) == 1
    for i in range(1, 256):
        state.record_block("t", "r", "tool", {"a": i})
    assert state.record_block("t", "r", "tool", {"a": 0}) == 2
    for i in range(256, 600):
        state.record_block("t", "r", "tool", {"a": i})
    assert state.record_block("t", "r", "tool", {"a": 0}) == 1  # idle too long: dropped, restarts


def test_state_holds_only_hashes_of_the_blocked_arguments():
    # Breaks if the full arguments are kept per thread forever.
    state = GuardState()
    state.record_block("t", "r", "tool", {"secret": "x" * 100_000})
    (key,) = state._threads["t"].blocks
    assert isinstance(key, str) and len(key) <= 64


def test_the_same_call_under_two_rules_counts_separately():
    state = GuardState()
    assert state.record_block("t", "r1", "tool", {"a": 1}) == 1
    assert state.record_block("t", "r2", "tool", {"a": 1}) == 1
    assert state.record_block("t", "r1", "tool", {"b": 1, "a": 2}) == 1
    assert state.record_block("t", "r1", "tool", {"a": 2, "b": 1}) == 2


def test_touching_10001_threads_drops_the_first_threads_counters_and_approvals():
    # Breaks if threads are kept forever: the first thread's state must be gone.
    state = GuardState()
    assert MAX_THREADS == 10_000
    state.record_block("thread-0", "r", "tool", {})
    assert state.record_block("thread-0", "r", "tool", {}) == 2
    state.record_approval("thread-0", "ask")
    for i in range(1, 10_001):
        state.record_block(f"thread-{i}", "r", "tool", {})
    assert not state.has_approval("thread-0", "ask")
    assert state.record_block("thread-0", "r", "tool", {}) == 1


def test_a_recently_used_thread_survives_the_thread_cap():
    # Breaks if the cap drops the oldest-created thread instead of the least recently used.
    state = GuardState()
    state.record_approval("busy", "ask")
    for i in range(1, 10_000):
        state.record_block(f"thread-{i}", "r", "tool", {})
    assert state.has_approval("busy", "ask")  # touched: now the newest
    state.record_block("one-more", "r", "tool", {})
    assert state.has_approval("busy", "ask")
    assert state.record_block("thread-1", "r", "tool", {}) == 1  # the least recently used went


def test_has_approval_of_an_unknown_thread_does_not_create_it():
    state = GuardState()
    assert not state.has_approval("nobody", "ask")
    assert "nobody" not in state._threads


# F3. A very long path cannot make the glob walk explode


def test_a_1000_segment_path_against_a_wildcard_glob_returns_fast_and_is_blocked():
    # Breaks if the walk has no memo and no cap: 1000 segments against four wildcard segments is
    # ~1000^4 steps, or a RecursionError.
    path = "/".join(["a"] * 1000)
    start = time.perf_counter()
    result = glob_may_match_below("*/*/*/*/zzz", path)
    assert time.perf_counter() - start < 2
    assert result is True  # fail closed


def test_a_256_segment_path_against_a_failing_wildcard_glob_walks_fast_with_the_memo():
    # Breaks if the walk is not memoised: every way to spread four a* segments over 256 segments
    # (about 256^4 / 24 of them) is tried before the walk can say no.
    path = "/".join(["a"] * 255 + ["c"])
    start = time.perf_counter()
    result = glob_may_match_below("a*/a*/a*/a*/b*", path)
    assert time.perf_counter() - start < 2
    assert result is False


def test_a_path_with_257_segments_is_over_the_limit_and_one_with_256_is_not():
    assert glob_may_match_below("x/y", "/".join(["a"] * 256)) is False
    assert glob_may_match_below("x/y", "/".join(["a"] * 257)) is True


@pytest.mark.parametrize("tool", ["write_file", "delete"])
def test_an_over_long_path_is_blocked_with_its_reason_and_the_tool_does_not_run(
    tmp_path, repo, tool
):
    # Breaks if the over-long path is let through to the walk, or blocked without saying why.
    _, mw = make(tmp_path)
    ran = Tool()
    start = time.perf_counter()
    with thread():
        result = run(mw, ran, tool, {"file_path": "/".join(["a"] * 1000)})
    assert time.perf_counter() - start < 2
    assert ran.ran == []
    assert result.status == "error"
    assert result.content == (
        "Blocked by rule no-migrations: Never touch the migrations folder."
        f" (tool {tool}: path has too many segments (more than 256))"
    )


def test_a_normal_path_is_unchanged_by_the_limit(tmp_path, repo):
    _, mw = make(tmp_path)
    ran = Tool()
    deep = "app/" + "/".join(["a"] * 200)
    with thread():
        run(mw, ran, "write_file", {"file_path": deep})
        bad = run(mw, ran, "write_file", {"file_path": "migrations/001.sql"}, "c2")
    assert ran.ran == [{"file_path": deep}]
    assert bad.content == "Blocked by rule no-migrations: Never touch the migrations folder."
