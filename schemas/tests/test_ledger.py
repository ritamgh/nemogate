import re

import pytest
import yaml
from pydantic import ValidationError

from schemas.enums import Authority, ConstraintKind, FileOp, Origin, Source
from schemas.ledger import (
    ApprovalBefore,
    Constraint,
    FilesFrozen,
    Ledger,
    OutputMustInclude,
    OutputMustNotInclude,
    PathForbidden,
    ToolArgBound,
    ToolForbidden,
    load_ledger_yaml,
)

# Verbatim from docs/SPEC.md, "Ledger file".
SPEC_LEDGER_YAML = """\
version: 1
constraints:
  - id: C-001
    text: "Do not modify anything under migrations/"
    kind: constraint        # constraint | scope | fact
    source: user            # user | system | tool | agent_inference
    authority: binding      # binding (block) | advisory (warn)
    scope: [coder, reviewer]
    origin: declared        # declared | extracted
    check:                  # null if not compilable -> reported as unverified
      type: path_forbidden
      glob: "migrations/**"
      ops: [write, delete]
"""


def constraint_dict(**overrides):
    base = {
        "id": "C-001",
        "text": "Some rule",
        "kind": "constraint",
        "source": "user",
        "authority": "binding",
        "scope": ["coder"],
        "origin": "declared",
        "check": {"type": "path_forbidden", "glob": "a/**", "ops": ["write"]},
    }
    base.update(overrides)
    return base


def ledger_dict(*constraints):
    return {"version": 1, "constraints": list(constraints) or [constraint_dict()]}


def test_spec_yaml_example_loads_with_path_forbidden_check():
    # Catches drift between the schema and the spec's ledger example.
    ledger = load_ledger_yaml(SPEC_LEDGER_YAML)
    entry = ledger.get("C-001")
    assert entry.text == "Do not modify anything under migrations/"
    assert entry.kind is ConstraintKind.constraint
    assert entry.source is Source.user
    assert entry.authority is Authority.binding
    assert entry.scope == ("coder", "reviewer")
    assert entry.origin is Origin.declared
    assert isinstance(entry.check, PathForbidden)
    assert entry.check.glob == "migrations/**"
    assert entry.check.ops == (FileOp.write, FileOp.delete)


@pytest.mark.parametrize(
    ("check", "model"),
    [
        ({"type": "path_forbidden", "glob": "x/**", "ops": ["delete"]}, PathForbidden),
        ({"type": "files_frozen", "globs": ["a", "b/**"]}, FilesFrozen),
        ({"type": "tool_forbidden", "name": "danger"}, ToolForbidden),
        ({"type": "approval_before", "tool": "push"}, ApprovalBefore),
        ({"type": "output_must_include", "regex": "^OK", "agent": "reviewer"}, OutputMustInclude),
        (
            {"type": "output_must_not_include", "regex": "secret", "agent": "reviewer"},
            OutputMustNotInclude,
        ),
        (
            {"type": "tool_arg_bound", "tool": "fetch", "arg": "retries", "op": "<=", "value": 3},
            ToolArgBound,
        ),
    ],
)
def test_each_predicate_type_parses_to_its_model(check, model):
    # Catches a broken discriminator or a missing union member.
    ledger = Ledger.model_validate(ledger_dict(constraint_dict(check=check)))
    assert type(ledger.constraints[0].check) is model


def test_files_frozen_and_tool_arg_bound_keep_their_values():
    frozen = Constraint.model_validate(
        constraint_dict(check={"type": "files_frozen", "globs": ["a", "b/**"]})
    )
    assert frozen.check.globs == ("a", "b/**")
    bound = Constraint.model_validate(
        constraint_dict(
            check={"type": "tool_arg_bound", "tool": "t", "arg": "n", "op": ">=", "value": 2.5}
        )
    )
    assert (bound.check.tool, bound.check.arg, bound.check.op, bound.check.value) == (
        "t",
        "n",
        ">=",
        2.5,
    )


def test_unknown_check_type_is_rejected():
    # Catches a non-closed predicate list.
    with pytest.raises(ValidationError):
        Ledger.model_validate(ledger_dict(constraint_dict(check={"type": "made_up", "glob": "x"})))


def test_extra_key_on_constraint_is_rejected():
    # Catches a typo'd ledger key being silently ignored.
    with pytest.raises(ValidationError):
        Ledger.model_validate(ledger_dict(constraint_dict(severity="high")))


def test_extra_key_inside_check_is_rejected():
    with pytest.raises(ValidationError):
        Ledger.model_validate(
            ledger_dict(
                constraint_dict(
                    check={"type": "path_forbidden", "glob": "a", "ops": ["write"], "extra": 1}
                )
            )
        )


def test_extra_top_level_key_is_rejected():
    with pytest.raises(ValidationError):
        Ledger.model_validate({**ledger_dict(), "note": "x"})


def test_duplicate_constraint_ids_are_rejected():
    # Catches ids being reused: the spec says ids are never reused.
    with pytest.raises(ValidationError, match="C-001"):
        Ledger.model_validate(ledger_dict(constraint_dict(), constraint_dict(text="Other")))


@pytest.mark.parametrize("regex", ["(unclosed", "[a-"])
@pytest.mark.parametrize("kind", ["output_must_include", "output_must_not_include"])
def test_invalid_regex_is_rejected(kind, regex):
    # Catches an uncompilable regex reaching the oracle at evaluation time.
    with pytest.raises(ValidationError):
        Ledger.model_validate(
            ledger_dict(constraint_dict(check={"type": kind, "regex": regex, "agent": "reviewer"}))
        )


@pytest.mark.parametrize(
    "regex",
    ["a{4294967296}", "(" * 20000 + ")" * 20000],
    ids=["repeat-count-overflow", "nesting-recursion"],
)
@pytest.mark.parametrize("kind", ["output_must_include", "output_must_not_include"])
def test_regex_that_crashes_the_compiler_is_a_validation_error(kind, regex):
    # Breaks if only re.error is caught: re.compile raises OverflowError / RecursionError
    # for these, which would escape as a crash instead of a ValidationError.
    with pytest.raises(ValidationError, match="invalid regex"):
        Ledger.model_validate(
            ledger_dict(constraint_dict(check={"type": kind, "regex": regex, "agent": "reviewer"}))
        )


def test_version_other_than_1_is_rejected():
    with pytest.raises(ValidationError):
        Ledger.model_validate({"version": 2, "constraints": [constraint_dict()]})


@pytest.mark.parametrize("bad_id", ["C1", "C-01", "X-001", "C-abc"])
def test_bad_constraint_id_is_rejected(bad_id):
    with pytest.raises(ValidationError):
        Ledger.model_validate(ledger_dict(constraint_dict(id=bad_id)))


def test_empty_ops_is_rejected():
    # Catches a path_forbidden that forbids nothing.
    with pytest.raises(ValidationError):
        Ledger.model_validate(
            ledger_dict(constraint_dict(check={"type": "path_forbidden", "glob": "a", "ops": []}))
        )


def test_empty_globs_and_empty_scope_and_empty_text_are_rejected():
    for bad in (
        constraint_dict(check={"type": "files_frozen", "globs": []}),
        constraint_dict(scope=[]),
        constraint_dict(text=""),
        constraint_dict(check={"type": "path_forbidden", "glob": "", "ops": ["write"]}),
        constraint_dict(check={"type": "tool_forbidden", "name": ""}),
        constraint_dict(check={"type": "approval_before", "tool": ""}),
    ):
        with pytest.raises(ValidationError):
            Ledger.model_validate(ledger_dict(bad))


def test_null_check_loads_as_unverified_constraint():
    ledger = load_ledger_yaml(
        """\
version: 1
constraints:
  - id: C-002
    text: "Be polite"
    kind: scope
    source: system
    authority: advisory
    scope: [planner]
    origin: extracted
    check: null
"""
    )
    assert ledger.get("C-002").check is None


def test_missing_check_key_defaults_to_none():
    entry = constraint_dict()
    del entry["check"]
    assert Ledger.model_validate(ledger_dict(entry)).constraints[0].check is None


def test_yaml_regex_with_inline_flag_survives_load_and_compiles():
    # Catches YAML quoting mangling a regex (e.g. escapes eaten) on the way in.
    ledger = load_ledger_yaml(
        """\
version: 1
constraints:
  - id: C-003
    text: "Reviews must not start with a risk marker"
    kind: constraint
    source: user
    authority: binding
    scope: [reviewer]
    origin: declared
    check:
      type: output_must_not_include
      regex: "(?m)^RISK:"
      agent: reviewer
"""
    )
    check = ledger.get("C-003").check
    assert check.regex == "(?m)^RISK:"
    assert re.compile(check.regex).search("ok\nRISK: high") is not None


def test_enums_dump_to_plain_strings_in_json_mode():
    # Catches StrEnum members leaking as "Authority.binding" in JSON output.
    dumped = load_ledger_yaml(SPEC_LEDGER_YAML).get("C-001").model_dump(mode="json")
    assert dumped["kind"] == "constraint"
    assert dumped["source"] == "user"
    assert dumped["authority"] == "binding"
    assert dumped["origin"] == "declared"
    assert dumped["check"] == {
        "type": "path_forbidden",
        "glob": "migrations/**",
        "ops": ["write", "delete"],
    }


def test_dumped_ledger_round_trips():
    ledger = load_ledger_yaml(SPEC_LEDGER_YAML)
    again = load_ledger_yaml(yaml.safe_dump(ledger.model_dump(mode="json")))
    assert again == ledger


def test_models_are_frozen():
    # Catches mutable models: the ledger hash must stay valid after loading.
    constraint = load_ledger_yaml(SPEC_LEDGER_YAML).get("C-001")
    with pytest.raises(ValidationError):
        constraint.text = "changed"
    with pytest.raises(ValidationError):
        constraint.check.glob = "other"


def test_ledger_get_returns_entry_and_raises_keyerror_for_unknown_id():
    ledger = Ledger.model_validate(
        ledger_dict(constraint_dict(), constraint_dict(id="C-002", text="Second"))
    )
    assert ledger.get("C-002").text == "Second"
    with pytest.raises(KeyError):
        ledger.get("C-003")


def test_frozen_ledger_has_no_mutable_lists():
    # Breaks if a list field comes back: `.append`/`.clear` on a frozen model would
    # change a validated ledger without re-validation (e.g. dropping every constraint).
    ledger = load_ledger_yaml(SPEC_LEDGER_YAML)
    entry = ledger.constraints[0]
    with pytest.raises(AttributeError):
        ledger.constraints.append(entry)
    with pytest.raises(AttributeError):
        entry.scope.clear()
    with pytest.raises(AttributeError):
        entry.check.ops.clear()
    frozen = FilesFrozen(globs=["a"])
    with pytest.raises(AttributeError):
        frozen.globs.clear()


def test_ledger_with_tuple_fields_still_dumps_json_arrays():
    # Breaks if the tuple fields stop serialising as JSON arrays (a reload would then fail).
    ledger = load_ledger_yaml(SPEC_LEDGER_YAML)
    dumped = ledger.model_dump_json()
    assert '"scope":["coder","reviewer"]' in dumped
    assert '"ops":["write","delete"]' in dumped
    assert Ledger.model_validate_json(dumped) == ledger
