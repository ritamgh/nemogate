"""The three cross-owner function signatures, as typing Protocols (docs/SPEC.md).

Owners: `EvaluateFn` is implemented in oracle/ (B), `RunFn` in replay/ (A) and
`MakeMiddlewareFn` in gate/ (B). Changing any of these needs the other owner's approval.
These are signatures only; there are no implementations here.
"""

from typing import Any, Protocol, runtime_checkable

from schemas.enums import Arm, Operator
from schemas.ledger import Ledger
from schemas.run import FsChange, RunConfig, RunRecord, Violation
from schemas.trace import Event


@runtime_checkable
class EvaluateFn(Protocol):
    def __call__(
        self, ledger: Ledger, trace: list[Event], fs_diff: list[FsChange]
    ) -> list[Violation]: ...


@runtime_checkable
class RunFn(Protocol):
    def __call__(
        self,
        task_ids: list[str],
        arms: list[Arm],
        operators: list[Operator],
        n: int,
        repo_ref: str = "main",
    ) -> list[RunRecord]: ...


@runtime_checkable
class MakeMiddlewareFn(Protocol):
    # list[AgentMiddleware] once docs/FACTS.md verifies the import.
    def __call__(self, run_config: RunConfig) -> list[Any]: ...
