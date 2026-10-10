"""Run formats: filesystem diff, violations, run config and run record (docs/SPEC.md)."""

from typing import Annotated, Self

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from schemas.enums import (
    STRICT,
    Arm,
    BoundaryId,
    ConstraintId,
    FsOp,
    NonEmptyStr,
    Operator,
    Role,
)


class FsChange(BaseModel):
    """One changed path between the base and final images of a run."""

    model_config = STRICT
    path: NonEmptyStr
    op: FsOp


FS_DIFF = TypeAdapter(list[FsChange])


class Violation(BaseModel):
    """A broken constraint, as returned by the oracle."""

    model_config = STRICT
    constraint_id: ConstraintId
    step: int | None  # None for post-hoc checks that have no single step
    predicate: str  # the check's type literal
    detail: str


class RunConfig(BaseModel):
    """Written by A's runner, read by B's gate at startup."""

    model_config = STRICT
    run_id: NonEmptyStr
    task_id: NonEmptyStr
    arm: Arm
    operator: Operator
    target_boundary: BoundaryId
    target_constraint: ConstraintId
    repo_ref: str = "main"
    ledger_path: NonEmptyStr
    trace_path: NonEmptyStr
    seed: int


class RunRecord(BaseModel):
    """One per run, written by A's replay engine."""

    model_config = STRICT
    run_id: NonEmptyStr
    task_id: NonEmptyStr
    arm: Arm
    operator: Operator
    constraint_id: ConstraintId
    boundary: BoundaryId
    violations: tuple[ConstraintId, ...]
    task_success: bool
    tokens: dict[Role, Annotated[int, Field(ge=0)]]
    cost_usd: float = Field(ge=0, allow_inf_nan=False)
    checkpoint_id: str | None
    contree_image: str | None
    model_ids: dict[Role, NonEmptyStr]
    safe_config_hash: NonEmptyStr
    ledger_hash: NonEmptyStr
    repo_sha: NonEmptyStr
    lockfile_hash: NonEmptyStr

    @model_validator(mode="after")
    def _per_role_fields_cover_every_role(self) -> Self:
        for name, mapping in (("model_ids", self.model_ids), ("tokens", self.tokens)):
            missing = sorted(role.value for role in Role if role not in mapping)
            if missing:
                raise ValueError(f"{name} is missing roles: {', '.join(missing)}")
        return self


def is_comparable(a: RunRecord, b: RunRecord) -> bool:
    """True if two run records were produced under the same conditions and may be pooled.

    SPEC "Reproducibility fields": two runs are comparable only if model_ids,
    safe_config_hash, ledger_hash, repo_sha and lockfile_hash match.
    Strict: the whole model_ids dict and all four hashes must match; pre- and post-repair runs
    (different repo_sha) never pool.
    """
    return (
        a.model_ids == b.model_ids
        and a.safe_config_hash == b.safe_config_hash
        and a.ledger_hash == b.ledger_hash
        and a.repo_sha == b.repo_sha
        and a.lockfile_hash == b.lockfile_hash
    )
