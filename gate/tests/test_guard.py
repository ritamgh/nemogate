"""Guard rules file loader, per-thread state store and GuardLoopError (no LangGraph wiring)."""

import logging
import threading

import pytest

from gate.errors import GuardLoopError
from gate.guard import Guard
from gate.state import GuardState
from schemas.ledger import ApprovalBefore, PathForbidden, ToolArgBound, ToolForbidden

MINIMAL = """\
version: 1
approval_tool: ask_human
tools:
  save_note: {path_arg: file, op: write}
rules:
  - id: no-migrations
    text: Never touch the migrations folder.
    check:
      path_forbidden: {glob: "migrations/**", ops: [write, delete]}
  - id: no-shell
    text: Do not run shell commands.
    mode: observe
    check:
      tool_forbidden: {name: run_shell}
  - id: ask-before-delete
    text: Ask a human before deleting.
    check:
      approval_before: {tool: delete_file}
  - id: small-pages
    text: Page size stays at 50 or below.
    check:
      tool_arg_bound: {tool: fetch, arg: page_size, op: "<=", value: 50}
"""


def write_rules(tmp_path, text):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_file_loads_one_rule_of_each_check_type_with_defaults_filled(tmp_path):
    guard = Guard.from_yaml(write_rules(tmp_path, MINIMAL))

    assert [r.id for r in guard.rules] == [
        "no-migrations",
        "no-shell",
        "ask-before-delete",
        "small-pages",
    ]
    assert [type(r.constraint.check) for r in guard.rules] == [
        PathForbidden,
        ToolForbidden,
        ApprovalBefore,
        ToolArgBound,
    ]
    first = guard.rules[0].constraint
    assert first.check.glob == "migrations/**"
    assert [op.value for op in first.check.ops] == ["write", "delete"]
    assert first.text == "Never touch the migrations folder."
    # What the file leaves out is filled with fixed defaults.
    assert (first.kind.value, first.source.value, first.authority.value, first.origin.value) == (
        "constraint",
        "user",
        "binding",
        "declared",
    )
    assert first.scope == ("all",)
    assert [r.mode for r in guard.rules] == ["block", "observe", "block", "block"]
    assert guard.rules[3].constraint.check.value == 50
    assert guard.approval_tool == "ask_human"
    assert guard.tool_map == {"save_note": {"path_arg": "file", "op": "write"}}
    assert isinstance(guard.state, GuardState)


def test_optional_top_level_keys_default_to_none_and_empty(tmp_path):
    text = """\
version: 1
rules:
  - id: no-shell
    text: Do not run shell commands.
    check: {tool_forbidden: {name: run_shell}}
"""
    guard = Guard.from_yaml(write_rules(tmp_path, text))

    assert guard.approval_tool is None
    assert guard.tool_map == {}


def test_each_guard_gets_its_own_state(tmp_path):
    path = write_rules(tmp_path, MINIMAL)

    assert Guard.from_yaml(path).state is not Guard.from_yaml(path).state


def test_unknown_rule_field_names_file_rule_and_field(tmp_path):
    # Catches a typo like `mood: observe` being silently ignored.
    text = MINIMAL.replace("    mode: observe\n", "    mode: observe\n    severity: high\n")
    path = write_rules(tmp_path, text)

    with pytest.raises(ValueError) as exc:
        Guard.from_yaml(path)

    message = str(exc.value)
    assert str(path) in message
    assert "no-shell" in message
    assert "severity" in message


def test_unknown_check_parameter_names_file_rule_and_field(tmp_path):
    text = MINIMAL.replace("{name: run_shell}", "{name: run_shell, glob: x}")
    path = write_rules(tmp_path, text)

    with pytest.raises(ValueError) as exc:
        Guard.from_yaml(path)

    message = str(exc.value)
    assert str(path) in message
    assert "no-shell" in message
    assert "glob" in message


def test_unknown_top_level_field_names_file_and_field(tmp_path):
    path = write_rules(tmp_path, MINIMAL + "extra_setting: 1\n")

    with pytest.raises(ValueError) as exc:
        Guard.from_yaml(path)

    assert str(path) in str(exc.value)
    assert "extra_setting" in str(exc.value)


def test_approval_before_without_approval_tool_names_the_rule(tmp_path):
    # Catches an approval rule that could never be satisfied loading without complaint.
    text = MINIMAL.replace("approval_tool: ask_human\n", "")
    path = write_rules(tmp_path, text)

    with pytest.raises(ValueError) as exc:
        Guard.from_yaml(path)

    assert str(path) in str(exc.value)
    assert "ask-before-delete" in str(exc.value)
    assert "approval_tool" in str(exc.value)


def test_rule_with_two_checks_is_rejected(tmp_path):
    text = MINIMAL.replace(
        "      tool_forbidden: {name: run_shell}\n",
        "      tool_forbidden: {name: run_shell}\n      approval_before: {tool: x}\n",
    )

    with pytest.raises(ValueError, match="no-shell.*exactly one check"):
        Guard.from_yaml(write_rules(tmp_path, text))


def test_rule_without_text_is_rejected_naming_the_rule(tmp_path):
    text = MINIMAL.replace("    text: Do not run shell commands.\n", "")

    with pytest.raises(ValueError, match="no-shell.*text"):
        Guard.from_yaml(write_rules(tmp_path, text))


def test_invalid_check_value_names_file_and_rule(tmp_path):
    text = MINIMAL.replace("ops: [write, delete]", "ops: [explode]")
    path = write_rules(tmp_path, text)

    with pytest.raises(ValueError) as exc:
        Guard.from_yaml(path)

    assert str(path) in str(exc.value)
    assert "no-migrations" in str(exc.value)


def test_duplicate_rule_ids_are_rejected(tmp_path):
    text = MINIMAL.replace("id: no-shell", "id: no-migrations")

    with pytest.raises(ValueError, match="no-migrations"):
        Guard.from_yaml(write_rules(tmp_path, text))


def test_tool_map_entry_with_unknown_field_is_rejected(tmp_path):
    text = MINIMAL.replace("{path_arg: file, op: write}", "{path_arg: file, op: write, x: 1}")
    path = write_rules(tmp_path, text)

    with pytest.raises(ValueError) as exc:
        Guard.from_yaml(path)

    assert str(path) in str(exc.value)
    assert "save_note" in str(exc.value)


def test_version_other_than_1_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="version"):
        Guard.from_yaml(write_rules(tmp_path, MINIMAL.replace("version: 1", "version: 2")))


def test_middleware_is_not_wired_yet(tmp_path):
    guard = Guard.from_yaml(write_rules(tmp_path, MINIMAL))

    with pytest.raises(NotImplementedError, match="wired in T-001b"):
        guard.middleware()


# GuardState


def test_identical_blocked_call_counts_one_then_two():
    state = GuardState()

    assert state.record_block("t", "r1", "save_note", {"file": "a"}) == 1
    assert state.record_block("t", "r1", "save_note", {"file": "a"}) == 2


def test_changed_argument_starts_again_at_one():
    state = GuardState()
    state.record_block("t", "r1", "save_note", {"file": "a"})
    state.record_block("t", "r1", "save_note", {"file": "a"})

    assert state.record_block("t", "r1", "save_note", {"file": "b"}) == 1


def test_other_rule_or_tool_has_its_own_count():
    state = GuardState()
    state.record_block("t", "r1", "save_note", {"file": "a"})

    assert state.record_block("t", "r2", "save_note", {"file": "a"}) == 1
    assert state.record_block("t", "r1", "write_file", {"file": "a"}) == 1


def test_argument_key_order_does_not_change_the_count():
    state = GuardState()
    state.record_block("t", "r1", "fetch", {"url": "u", "page": 1})

    assert state.record_block("t", "r1", "fetch", {"page": 1, "url": "u"}) == 2


def test_canonical_args_is_sorted_json_and_falls_back_to_str():
    from pathlib import PurePosixPath

    assert GuardState().canonical_args({"b": 1, "a": PurePosixPath("x")}) == '{"a": "x", "b": 1}'


def test_threads_share_no_counters():
    state = GuardState()
    state.record_block("A", "r1", "save_note", {"file": "a"})
    assert state.record_block("A", "r1", "save_note", {"file": "a"}) == 2

    assert state.record_block("B", "r1", "save_note", {"file": "a"}) == 1


def test_approvals_are_kept_per_thread_and_rule():
    state = GuardState()
    assert state.has_approval("A", "r1") is False

    state.record_approval("A", "r1")

    assert state.has_approval("A", "r1") is True
    assert state.has_approval("B", "r1") is False
    assert state.has_approval("A", "r2") is False


def test_thread_key_reads_thread_id_from_the_config():
    assert GuardState().thread_key({"configurable": {"thread_id": "t-9"}}) == "t-9"


def test_missing_thread_id_is_the_default_thread_with_exactly_one_warning(caplog):
    state = GuardState()

    with caplog.at_level(logging.WARNING, logger="gate.state"):
        keys = [
            state.thread_key({}),
            state.thread_key({"configurable": {}}),
            state.thread_key({"configurable": {"thread_id": None}}),
            state.thread_key({"configurable": {"thread_id": "t-1"}}),
        ]

    assert keys == ["default", "default", "default", "t-1"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "thread_id" in warnings[0].getMessage()


def test_each_state_warns_once_for_itself(caplog):
    with caplog.at_level(logging.WARNING, logger="gate.state"):
        GuardState().thread_key({})
        GuardState().thread_key({})

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_concurrent_record_block_loses_no_count():
    # Catches a counter incremented without the lock (read-modify-write race).
    state = GuardState()
    results = []

    def worker():
        for _ in range(25):
            results.append(state.record_block("t", "r1", "save_note", {"file": "a"}))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max(results) == 200
    assert sorted(results) == list(range(1, 201))


# GuardLoopError


def test_guard_loop_error_names_the_rule_and_carries_its_fields():
    error = GuardLoopError("no-migrations", "save_note", "thread-7")

    assert isinstance(error, RuntimeError)
    assert (error.rule_id, error.tool, error.thread_id) == (
        "no-migrations",
        "save_note",
        "thread-7",
    )
    assert "no-migrations" in str(error)
    assert "save_note" in str(error)
