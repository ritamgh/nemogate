"""The rules file end to end: the minimal fixture, every loader error, and glob normalisation."""

from pathlib import Path

import pytest

from gate.guard import Guard
from oracle import glob_matches
from schemas.ledger import ApprovalBefore, PathForbidden, ToolForbidden

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "rules_minimal.yaml"

RULES_HEAD = "version: 1\nrules:\n"


def write_rules(tmp_path, text):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def one_path_rule(glob, rule_id="no-secrets"):
    return (
        f"{RULES_HEAD}  - id: {rule_id}\n    text: Keep out.\n"
        f'    check:\n      path_forbidden: {{glob: "{glob}", ops: [write]}}\n'
    )


def load_error(path):
    with pytest.raises(ValueError) as exc:
        Guard.from_yaml(path)
    return str(exc.value)


# The fixture


def test_minimal_fixture_loads_every_rule_with_every_default_filled():
    # Breaks if the loader stops filling a ledger field, or stops accepting a check type.
    guard = Guard.from_yaml(FIXTURE)

    assert [r.id for r in guard.rules] == ["no-secrets-folder", "no-shell", "ask-before-purge"]
    assert [type(r.constraint.check) for r in guard.rules] == [
        PathForbidden,
        ToolForbidden,
        ApprovalBefore,
    ]
    assert [r.mode for r in guard.rules] == ["block", "observe", "block"]
    for rule in guard.rules:
        c = rule.constraint
        assert (c.kind.value, c.source.value, c.authority.value, c.origin.value) == (
            "constraint",
            "user",
            "binding",
            "declared",
        )
        assert c.scope == ("all",)
    path_rule = guard.rules[0].constraint
    assert path_rule.check.glob == "secrets/**"
    assert [op.value for op in path_rule.check.ops] == ["write", "delete"]
    assert guard.rules[1].constraint.check.name == "run_shell"
    assert guard.rules[2].constraint.check.tool == "purge_records"
    assert guard.approval_tool == "ask_human"
    assert guard.tool_map == {"save_note": {"path_arg": "file", "op": "write"}}


# Each error names the file, the rule and the field


def test_unknown_top_level_field_names_file_and_field(tmp_path):
    path = write_rules(tmp_path, FIXTURE.read_text() + "retries: 3\n")

    message = load_error(path)

    assert str(path) in message
    assert "unknown field 'retries'" in message


def test_unknown_rule_field_names_file_rule_and_field(tmp_path):
    text = FIXTURE.read_text().replace("    mode: observe\n", "    mode: observe\n    level: 9\n")
    path = write_rules(tmp_path, text)

    message = load_error(path)

    assert str(path) in message
    assert "no-shell" in message
    assert "unknown field 'level'" in message


def test_unknown_check_param_names_file_rule_and_field(tmp_path):
    text = FIXTURE.read_text().replace("{tool: purge_records}", "{tool: purge_records, why: x}")
    path = write_rules(tmp_path, text)

    message = load_error(path)

    assert str(path) in message
    assert "ask-before-purge" in message
    assert "unknown field 'why'" in message


def test_unknown_tools_map_field_names_file_tool_and_field(tmp_path):
    text = FIXTURE.read_text().replace("op: write}", "op: write, mode: x}")
    path = write_rules(tmp_path, text)

    message = load_error(path)

    assert str(path) in message
    assert "save_note" in message
    assert "unknown field 'mode'" in message


def test_unknown_tools_map_op_names_file_and_tool(tmp_path):
    # Breaks if a tool mapped to an op the oracle does not know is accepted and never matches.
    text = FIXTURE.read_text().replace("{path_arg: file, op: write}", "{path_arg: file, op: wipe}")
    path = write_rules(tmp_path, text)

    message = load_error(path)

    assert str(path) in message
    assert "save_note" in message
    assert "op must be one of" in message


def test_tools_map_entry_without_path_arg_is_rejected(tmp_path):
    text = FIXTURE.read_text().replace("{path_arg: file, op: write}", "{op: write}")

    message = load_error(write_rules(tmp_path, text))

    assert "save_note" in message
    assert "path_arg is required" in message


def test_unknown_check_type_names_the_rule_and_the_choices(tmp_path):
    text = FIXTURE.read_text().replace("tool_forbidden:", "tool_banned:")

    message = load_error(write_rules(tmp_path, text))

    assert "no-shell" in message
    assert "unknown check 'tool_banned'" in message
    assert "path_forbidden" in message


# Structure


def test_duplicate_rule_ids_name_the_file_and_the_id(tmp_path):
    path = write_rules(
        tmp_path, FIXTURE.read_text().replace("id: no-shell", "id: no-secrets-folder")
    )

    message = load_error(path)

    assert str(path) in message
    assert "duplicate constraint id no-secrets-folder" in message


def test_two_checks_in_one_rule_are_rejected(tmp_path):
    text = FIXTURE.read_text().replace(
        "      tool_forbidden: {name: run_shell}\n",
        "      tool_forbidden: {name: run_shell}\n      approval_before: {tool: x}\n",
    )

    message = load_error(write_rules(tmp_path, text))

    assert "no-shell" in message
    assert "exactly one check" in message


@pytest.mark.parametrize("field", ["id", "text", "check"])
def test_rule_missing_a_required_field_names_the_field(tmp_path, field):
    lines = {
        "id": "id: no-shell",
        "text": "text: Do not run shell commands.",
        "check": "check: {tool_forbidden: {name: run_shell}}",
    }
    del lines[field]
    body = "".join(f"  {'-' if i == 0 else ' '} {line}\n" for i, line in enumerate(lines.values()))

    message = load_error(write_rules(tmp_path, RULES_HEAD + body))

    assert f"missing field '{field}'" in message


def test_approval_before_without_approval_tool_names_file_and_rule(tmp_path):
    path = write_rules(tmp_path, FIXTURE.read_text().replace("approval_tool: ask_human\n", ""))

    message = load_error(path)

    assert str(path) in message
    assert "ask-before-purge" in message
    assert "approval_tool" in message


@pytest.mark.parametrize("mode", ["warn", "Block", "off"])
def test_mode_other_than_block_or_observe_is_rejected(tmp_path, mode):
    text = FIXTURE.read_text().replace("mode: observe", f"mode: {mode}")

    message = load_error(write_rules(tmp_path, text))

    assert "no-shell" in message
    assert "mode must be one of ['block', 'observe']" in message


def test_empty_file_is_rejected_naming_the_file(tmp_path):
    path = write_rules(tmp_path, "")

    assert str(path) in load_error(path)
    assert "mapping" in load_error(path)


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "42\n"])
def test_non_mapping_yaml_is_rejected_naming_the_file(tmp_path, text):
    path = write_rules(tmp_path, text)

    message = load_error(path)

    assert str(path) in message
    assert "mapping" in message


def test_malformed_yaml_raises_a_value_error_naming_the_file(tmp_path):
    # Breaks if a YAML syntax error escapes as a bare yaml error with no file name.
    path = write_rules(tmp_path, "version: 1\nrules: [unclosed\n")

    assert str(path) in load_error(path)


# Rule ids


@pytest.mark.parametrize("rule_id", ["no-shell", "a", "rule-2", "C-001", "no-write-2-secrets"])
def test_slug_ids_are_accepted(tmp_path, rule_id):
    guard = Guard.from_yaml(write_rules(tmp_path, one_path_rule("secrets/**", rule_id)))

    assert [r.id for r in guard.rules] == [rule_id]


@pytest.mark.parametrize("rule_id", ["No-Shell", "no shell", "no_shell", "-lead", "trail-"])
def test_bad_ids_are_rejected_naming_the_file_and_the_id(tmp_path, rule_id):
    path = write_rules(tmp_path, one_path_rule("secrets/**", f'"{rule_id}"'))

    message = load_error(path)

    assert str(path) in message
    assert f"rule {rule_id!r}" in message
    assert "pattern" in message


# Globs


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        ("./app/**", "app/**"),
        (".//app/**", "app/**"),
        ("././app/**", "app/**"),
        ("app//deep///**", "app/deep/**"),
        ("app/./deep/**", "app/deep/**"),
        ("app/", "app/"),
        ("./app//", "app/"),
        ("**/*.env", "**/*.env"),
    ],
)
def test_glob_is_normalised_at_load_time(tmp_path, written, stored):
    # Breaks if the loader keeps a leading ./ : the oracle compares repo-relative paths, so
    # "./app/**" matches nothing and the rule silently protects nothing.
    guard = Guard.from_yaml(write_rules(tmp_path, one_path_rule(written)))

    assert guard.rules[0].constraint.check.glob == stored


def test_dot_slash_glob_protects_the_path_the_oracle_compares(tmp_path):
    # The point of normalising: the stored glob matches a repo-relative path.
    guard = Guard.from_yaml(write_rules(tmp_path, one_path_rule("./app/**")))

    assert glob_matches(guard.rules[0].constraint.check.glob, "app/main.py") is True


@pytest.mark.parametrize("glob", ["/app/**", "//app/**", "app/../secrets/**", "../x", "**/.."])
def test_absolute_glob_or_dotdot_segment_is_rejected_naming_file_and_rule(tmp_path, glob):
    path = write_rules(tmp_path, one_path_rule(glob))

    message = load_error(path)

    assert str(path) in message
    assert "no-secrets" in message
    assert "glob" in message


def test_dotdot_inside_a_segment_name_is_not_a_parent_reference(tmp_path):
    guard = Guard.from_yaml(write_rules(tmp_path, one_path_rule("a..b/**")))

    assert guard.rules[0].constraint.check.glob == "a..b/**"


def test_glob_that_normalises_to_nothing_is_rejected(tmp_path):
    path = write_rules(tmp_path, one_path_rule("./"))

    message = load_error(path)

    assert str(path) in message
    assert "no-secrets" in message
