"""The ledger: typed constraints and the closed predicate DSL (docs/SPEC.md, Shared interfaces)."""

import re
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from schemas.enums import (
    STRICT,
    AgentName,
    Authority,
    ConstraintId,
    ConstraintKind,
    FileOp,
    NonEmptyStr,
    Origin,
    Source,
)


def _check_regex(value: str) -> str:
    try:
        re.compile(value)
    except (re.error, OverflowError, RecursionError) as exc:
        # OverflowError: huge repeat count. RecursionError: very deep nesting.
        raise ValueError(f"invalid regex {value[:80]!r}: {type(exc).__name__}: {exc}") from exc
    return value


class PathForbidden(BaseModel):
    model_config = STRICT
    type: Literal["path_forbidden"] = "path_forbidden"
    glob: NonEmptyStr
    ops: tuple[FileOp, ...] = Field(min_length=1)


class FilesFrozen(BaseModel):
    model_config = STRICT
    type: Literal["files_frozen"] = "files_frozen"
    globs: tuple[str, ...] = Field(min_length=1)


class ToolForbidden(BaseModel):
    model_config = STRICT
    type: Literal["tool_forbidden"] = "tool_forbidden"
    name: NonEmptyStr


class ApprovalBefore(BaseModel):
    model_config = STRICT
    type: Literal["approval_before"] = "approval_before"
    tool: NonEmptyStr


class OutputMustInclude(BaseModel):
    model_config = STRICT
    type: Literal["output_must_include"] = "output_must_include"
    regex: str
    agent: AgentName

    _regex_compiles = field_validator("regex")(_check_regex)


class OutputMustNotInclude(BaseModel):
    model_config = STRICT
    type: Literal["output_must_not_include"] = "output_must_not_include"
    regex: str
    agent: AgentName

    _regex_compiles = field_validator("regex")(_check_regex)


class ToolArgBound(BaseModel):
    """Stretch predicate."""

    model_config = STRICT
    type: Literal["tool_arg_bound"] = "tool_arg_bound"
    tool: str
    arg: str
    op: Literal["<", "<=", "==", "!=", ">=", ">"]
    value: int | float | str


Check = Annotated[
    PathForbidden
    | FilesFrozen
    | ToolForbidden
    | ApprovalBefore
    | OutputMustInclude
    | OutputMustNotInclude
    | ToolArgBound,
    Field(discriminator="type"),
]


class Constraint(BaseModel):
    model_config = STRICT
    id: ConstraintId
    text: NonEmptyStr
    kind: ConstraintKind
    source: Source
    authority: Authority
    scope: tuple[AgentName, ...] = Field(min_length=1)
    origin: Origin
    check: Check | None = None  # None = not compilable, reported as unverified

    @model_validator(mode="after")
    def _check_agent_is_in_scope(self) -> "Constraint":
        if (
            isinstance(self.check, OutputMustInclude | OutputMustNotInclude)
            and self.check.agent not in self.scope
        ):
            raise ValueError(f"check agent {self.check.agent!r} is not in scope {list(self.scope)}")
        return self


class Ledger(BaseModel):
    model_config = STRICT
    version: Literal[1]
    constraints: tuple[Constraint, ...]

    @model_validator(mode="after")
    def _ids_are_unique(self) -> "Ledger":
        seen: set[str] = set()
        for constraint in self.constraints:
            if constraint.id in seen:
                raise ValueError(f"duplicate constraint id {constraint.id}")
            seen.add(constraint.id)
        return self

    def get(self, constraint_id: str) -> Constraint:
        for constraint in self.constraints:
            if constraint.id == constraint_id:
                return constraint
        raise KeyError(constraint_id)


def load_ledger_yaml(text: str) -> Ledger:
    return Ledger.model_validate(yaml.safe_load(text))
