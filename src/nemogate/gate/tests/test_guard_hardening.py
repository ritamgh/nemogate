"""Hardening found by the security audit: an approval tool's plain-text "no" is not an approval,
the per-thread counters and approvals are bounded in memory, and a very long path cannot make the
glob walk blow up (F1, F2, F3)."""

import time

import pytest
from langchain_core.messages import ToolMessage

from nemogate.gate.errors import GuardLoopError
from nemogate.gate.middleware import approval_granted, glob_may_match_below
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
    "Notice: approved by admin",
    "Note: nothing to decide",
    "Nobody objected",
    "",
    "Not now",
    "Permission denied",
    "\u201cNo\u201d",
    "No\u2014do not delete",
    "\u200bNo",
    "not\tapproved",
    "\u200bYes, not now",
]
APPROVALS = [
    "yes",
    "Approved",
    "ok, go ahead",
    "approved by admin",
    "\u201cYes\u201d",
    "\u200bYes",
    "Yes\u2014go ahead",
    "ok,\tgo",
    '{"approved": true}',
    '{"decision": "approved"}',
]


@pytest.mark.parametrize("text", DENIALS)
def test_a_denial_or_an_unknown_reply_is_not_recorded_as_an_approval(tmp_path, repo, text):
    # Breaks if anything but an explicit yes counts: "Denied by user", "" or "No" in curly
    # quotes would unlock drop_table.
    result, drop = approval_run(tmp_path, message(text))
    assert drop.ran == []
    assert result.content == DROP


@pytest.mark.parametrize("text", APPROVALS)
def test_an_explicit_yes_counts_as_an_approval(tmp_path, repo, text):
    # Breaks if an explicit yes (curly quotes, a zero-width space, an em dash, a tab) is not read
    # as one, so a human's approval never unlocks the tool.
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
    "result",
    [
        "yes",
        "Yes.",
        "Approved",
        "approved by admin",
        "ok, go ahead",
        "OK",
        {"approved": True},
        {"approved": "yes"},
        {"approved": "Approved."},
        {"decision": "approved"},
        {"status": "ok"},
        "true",
        '"yes"',
        "\u201cYes\u201d",
        "\u200bYes",
        "Yes\u2014go ahead",
        "ok,\tgo",
        ToolMessage(content="yes", tool_call_id="c", name="t"),
        ToolMessage(content='{"approved": true}', tool_call_id="c", name="t"),
        ToolMessage(content=[{"type": "text", "text": "Approved"}], tool_call_id="c"),
    ],
)
def test_approval_granted_accepts_an_explicit_yes(result):
    # Breaks if an explicit yes in any of its shapes stops unlocking the guarded tool.
    assert approval_granted(result) is True


@pytest.mark.parametrize(
    "result",
    [
        "Not now",
        "Permission denied",
        "User denied the request",
        "The user declined",
        "Nope",
        "Never",
        "Absolutely not",
        "not allowed",
        "",
        "   ",
        "no thanks",
        "Yes, but not now",
        "Yes, I don't allow it",
        "Denied by user",
        "Notice: approved by admin",
        "No problem, go ahead",
        "pending",
        "\u201cNo\u201d",
        "No\u2014do not delete",
        "\u200bNo",
        "not\tapproved",
        "\u200bYes, not now",
        "false",
        "0",
        "null",
        "1",
        '"No."',
        False,
        0,
        None,
        {"approved": "rejected"},
        {"approved": "No."},
        {"approved": None},
        {"approved": False},
        {"approved": 1},
        {"approved": "maybe"},
        {"status": "denied"},
        {"decision": "yes", "status": "denied"},
        {"approved": False, "decision": "approved"},
        {"reason": "no key"},
        {},
        ["approved", True],
        ToolMessage(content="boom", tool_call_id="c", name="t", status="error"),
        ToolMessage(content="yes", tool_call_id="c", name="t", status="error"),
        ToolMessage(content="No", tool_call_id="c", name="t"),
        ToolMessage(content=[{"type": "text", "text": "Declined"}], tool_call_id="c"),
        ToolMessage(content="", tool_call_id="c", name="t"),
    ],
)
def test_approval_granted_refuses_everything_but_an_explicit_yes(result):
    # Breaks if a denial wording, silence or an unknown reply is recorded as an approval.
    assert approval_granted(result) is False


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


def test_a_256_segment_path_against_a_wildcard_glob_walks_fast_with_the_memo():
    # Breaks if the walk is not memoised: every way to spread four a* segments over 256 segments
    # (about 256^4 / 24 of them) may be tried. The answer is True since * crosses "/": the path
    # a/a/.../a/c/b (below the target) matches a*/a*/a*/a*/b* under fnmatch, as the oracle reads it.
    path = "/".join(["a"] * 255 + ["c"])
    start = time.perf_counter()
    result = glob_may_match_below("a*/a*/a*/a*/b*", path)
    assert time.perf_counter() - start < 2
    assert result is True


def test_24_star_segments_against_24_non_matching_segments_return_fast():
    # Breaks if the walk backtracks without a memo: the reviewer's 81-character glob kept the
    # guard busy for over 3 seconds (0.9 s at 20 segments, doubling per segment).
    glob = "/".join(["a*"] * 24 + ["protected"])
    path = "/".join(["a"] * 24 + ["no-match"])
    start = time.perf_counter()
    result = glob_may_match_below(glob, path)
    assert time.perf_counter() - start < 1
    assert result is True  # the last a* may absorb no-match/..., so a match below can exist


def test_a_10000_character_segment_against_many_stars_returns_fast():
    # Breaks if matching one very long segment against a glob full of stars is not linear-ish.
    glob = "/".join(["a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*b"] * 3)
    path = "a" * 10_000 + "/" + "a" * 10_000
    start = time.perf_counter()
    glob_may_match_below(glob, path)
    assert time.perf_counter() - start < 1


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


# H3. A huge path is rejected before any filesystem work


def test_a_300000_segment_path_is_blocked_in_under_half_a_second(tmp_path, repo):
    # Breaks if the path is resolved on the filesystem before its size is checked: 300,000
    # segments took about 5.7 s to reject.
    _, mw = make(tmp_path)
    ran = Tool()
    start = time.perf_counter()
    with thread():
        result = run(mw, ran, "write_file", {"file_path": "/".join(["a"] * 300_000)})
    assert time.perf_counter() - start < 0.5
    assert ran.ran == []
    assert result.content == (
        "Blocked by rule no-migrations: Never touch the migrations folder."
        " (tool write_file: path has too many segments (more than 256))"
    )


def test_a_path_over_4096_characters_is_blocked_with_its_reason(tmp_path, repo):
    # Breaks if only the segment count is bounded: one 5000-character segment is cheap to count
    # but not a path to resolve.
    _, mw = make(tmp_path)
    ran = Tool()
    with thread():
        result = run(mw, ran, "write_file", {"file_path": "app/" + "a" * 5000})
        fine = run(mw, ran, "write_file", {"file_path": "app/" + "a" * 4000}, "c2")
    assert result.content == (
        "Blocked by rule no-migrations: Never touch the migrations folder."
        " (tool write_file: path is too long (more than 4096 characters))"
    )
    assert fine.content == "ran"


# H2. Deleting an existing plain file is judged by the direct glob match only

ENV_RULE = """\
version: 1
rules:
  - id: keep-env
    text: Keep env files.
    check:
      path_forbidden: {glob: "**/.env", ops: [delete]}
"""


@pytest.mark.parametrize(
    ("path", "blocked"),
    [
        ("README.md", False),  # an existing file: only the oracle's direct match applies
        ("src/a.py", False),
        (".env", True),  # the oracle's own match
        ("src/.env", True),
        ("src", True),  # an existing folder holding a .env below
        ("docs", True),  # an existing folder: kept conservative even though empty of matches
        ("no/such/path", True),  # not on disk: the old conservative answer
    ],
)
def test_deleting_an_existing_plain_file_is_not_blocked_by_a_glob_that_only_matches_below(
    tmp_path, repo, path, blocked
):
    # Breaks if a plain file is treated as a folder that could hold a .env: deleting README.md
    # was blocked although the oracle passes it.
    (repo / "src").mkdir()
    (repo / "docs").mkdir()
    (repo / "README.md").write_text("x", encoding="utf-8")
    (repo / "src" / "a.py").write_text("x", encoding="utf-8")
    (repo / "src" / ".env").write_text("", encoding="utf-8")
    (repo / ".env").write_text("", encoding="utf-8")
    _, mw = make(tmp_path, ENV_RULE)
    tool = Tool()
    with thread():
        result = run(mw, tool, "delete", {"file_path": path})
    assert (tool.ran == []) is blocked
    assert (result.content == "Blocked by rule keep-env: Keep env files.") is blocked
