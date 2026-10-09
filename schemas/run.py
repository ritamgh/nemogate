"""Run formats: filesystem diff, violations, run config and run record (docs/SPEC.md)."""

from typing import Annotated, Self

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from schemas.enums import STRICT, Arm, BoundaryId, ConstraintId, FsOp, Operator, Role

NonEmptyStr = Annotated[str, Field(min_length=1)]


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
    run_id: str
    task_id: str
    arm: Arm
    operator: Operator
    target_boundary: BoundaryId
    target_constraint: ConstraintId
    repo_ref: str = "main"
    ledger_path: str
    trace_path: str
    seed: int


class RunRecord(BaseModel):
    """One per run, written by A's replay engine."""

    model_config = STRICT
    run_id: str
    task_id: str
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
    model_ids: dict[Role, str]
    safe_config_hash: str
    ledger_hash: str
    repo_sha: str
    lockfile_hash: str

    @model_validator(mode="after")
    def _model_ids_cover_every_role(self) -> Self:
        missing = sorted(role.value for role in Role if role not in self.model_ids)
        if missing:
            raise ValueError(f"model_ids is missing roles: {', '.join(missing)}")
        return self
