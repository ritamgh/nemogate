import json

import pytest
from pydantic import ValidationError

from schemas.enums import Arm, FsOp, Operator, Role
from schemas.run import FS_DIFF, FsChange, RunConfig, RunRecord, Violation, is_comparable

# Verbatim from docs/SPEC.md, "Run config".
SPEC_RUN_CONFIG = (
    '{"run_id":"r-0042","task_id":"T3","arm":"mutated","operator":"hedge",'
    '"target_boundary":"delegation:planner->coder","target_constraint":"C-001",'
    '"repo_ref":"main","ledger_path":"nemogate.yaml","trace_path":"runs/r-0042/trace.jsonl",'
    '"seed":42}'
)

# Verbatim from docs/SPEC.md, "Filesystem diff".
SPEC_FS_DIFF = (
    '[{"path":"migrations/0003.py","op":"added"},{"path":"app/models.py","op":"modified"}]'
)

# From docs/SPEC.md, "Run record"; only the tokens keys are renamed to roles (ruling R3).
SPEC_RUN_RECORD = (
    '{"run_id":"r-0042","task_id":"T3","arm":"mutated","operator":"hedge",'
    '"constraint_id":"C-001","boundary":"delegation:planner->coder","violations":["C-001"],'
    '"task_success":false,"tokens":{"helper":1200,"sut":18400,"planner":0},"cost_usd":0.011,'
    '"checkpoint_id":"…","contree_image":"…","model_ids":{"helper":"nvidia/nemotron-3-nano-30b-a3b",'
    '"sut":"nvidia/nemotron-3-super-120b-a12b","planner":"nvidia/nemotron-3-ultra-550b-a55b"},'
    '"safe_config_hash":"9c1f2a","ledger_hash":"41ab07","repo_sha":"3fa91c2",'
    '"lockfile_hash":"b27e5d"}'
)


def _record_data(**overrides):
    return {**json.loads(SPEC_RUN_RECORD), **overrides}


def test_spec_run_config_loads_verbatim():
    config = RunConfig.model_validate_json(SPEC_RUN_CONFIG)
    assert config.arm is Arm.mutated
    assert config.operator is Operator.hedge
    assert config.target_boundary == "delegation:planner->coder"
    assert config.target_constraint == "C-001"
    assert config.seed == 42


def test_run_config_repo_ref_defaults_to_main():
    # Breaks if the default moves off "main"; the repair agent relies on overriding it.
    data = json.loads(SPEC_RUN_CONFIG)
    del data["repo_ref"]
    assert RunConfig.model_validate(data).repo_ref == "main"


def test_run_config_with_unknown_key_is_rejected():
    with pytest.raises(ValidationError):
        RunConfig.model_validate({**json.loads(SPEC_RUN_CONFIG), "extra": 1})


def test_spec_fs_diff_loads_verbatim():
    diff = FS_DIFF.validate_json(SPEC_FS_DIFF)
    assert diff == [
        FsChange(path="migrations/0003.py", op=FsOp.added),
        FsChange(path="app/models.py", op=FsOp.modified),
    ]


def test_fs_change_with_unknown_op_is_rejected():
    # Breaks if the closed set of ops (added/modified/deleted) is loosened to any string.
    with pytest.raises(ValidationError):
        FsChange(path="a.py", op="renamed")


def test_fs_change_with_empty_path_is_rejected():
    with pytest.raises(ValidationError):
        FsChange(path="", op="added")


def test_spec_run_record_loads_with_role_keyed_tokens():
    record = RunRecord.model_validate_json(SPEC_RUN_RECORD)
    assert record.tokens == {Role.helper: 1200, Role.sut: 18400, Role.planner: 0}
    assert record.violations == ("C-001",)
    assert record.task_success is False
    assert record.cost_usd == 0.011
    assert record.model_ids[Role.sut] == "nvidia/nemotron-3-super-120b-a12b"


def test_run_record_missing_a_model_role_is_rejected():
    # Breaks if a record can omit a role's model id, making runs non-comparable.
    model_ids = {
        "helper": "nvidia/nemotron-3-nano-30b-a3b",
        "sut": "nvidia/nemotron-3-super-120b-a12b",
    }
    with pytest.raises(ValidationError, match="planner"):
        RunRecord.model_validate(_record_data(model_ids=model_ids))


@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_run_record_non_finite_cost_is_rejected(bad):
    # Breaks if inf is accepted: it dumps as JSON null, so the record cannot be reloaded.
    with pytest.raises(ValidationError):
        RunRecord.model_validate(_record_data(cost_usd=bad))


def test_run_record_negative_cost_is_rejected():
    with pytest.raises(ValidationError):
        RunRecord.model_validate(_record_data(cost_usd=-0.01))


def test_run_record_negative_token_count_is_rejected():
    with pytest.raises(ValidationError):
        RunRecord.model_validate(_record_data(tokens={"helper": -1, "sut": 0, "planner": 0}))


def test_run_record_tokens_keyed_by_old_tier_names_is_rejected():
    # Breaks if tokens goes back to nano/super/ultra keys (ruling R3: keyed by role).
    with pytest.raises(ValidationError):
        RunRecord.model_validate(_record_data(tokens={"nano": 1, "super": 2, "ultra": 3}))


def test_run_record_violation_id_must_be_a_constraint_id():
    # Breaks if violations accepts free strings instead of ids like C-001.
    with pytest.raises(ValidationError):
        RunRecord.model_validate(_record_data(violations=["C1"]))


def test_violation_without_step_is_valid():
    # Post-hoc checks (e.g. a filesystem diff) have no single step.
    violation = Violation(
        constraint_id="C-001", step=None, predicate="files_frozen", detail="changed: a.py"
    )
    assert violation.step is None


def test_violation_requires_the_step_key():
    # step is nullable but not optional: the oracle must say None explicitly.
    with pytest.raises(ValidationError):
        Violation.model_validate({"constraint_id": "C-001", "predicate": "p", "detail": "d"})


def test_run_record_violations_cannot_be_mutated_in_place():
    # Breaks if `violations` is a list again: a frozen record could then be edited unvalidated.
    record = RunRecord.model_validate_json(SPEC_RUN_RECORD)
    with pytest.raises(AttributeError):
        record.violations.append("C-002")
    assert '"violations":["C-001"]' in record.model_dump_json()


def test_records_differing_only_in_non_fingerprint_fields_are_comparable():
    # Breaks if is_comparable starts comparing run_id/arm/cost: arms must pool together.
    a = RunRecord.model_validate(_record_data())
    b = RunRecord.model_validate(
        _record_data(run_id="r-0043", arm="gated", cost_usd=0.5, task_success=True)
    )
    assert is_comparable(a, b) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "model_ids": {
                "helper": "nvidia/nemotron-3-nano-30b-a3b",
                "sut": "nvidia/nemotron-3-super-120b-a12b",
                "planner": "nvidia/nemotron-3-ultra-other",
            }
        },
        {"safe_config_hash": "ffffff"},
        {"ledger_hash": "ffffff"},
        {"repo_sha": "ffffff"},
        {"lockfile_hash": "ffffff"},
    ],
    ids=["planner_model_id", "safe_config_hash", "ledger_hash", "repo_sha", "lockfile_hash"],
)
def test_records_differing_in_one_fingerprint_field_are_not_comparable(overrides):
    # Breaks if any one fingerprint field is dropped from the comparison (strict rule).
    a = RunRecord.model_validate(_record_data())
    b = RunRecord.model_validate(_record_data(**overrides))
    assert is_comparable(a, b) is False
