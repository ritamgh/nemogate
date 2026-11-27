"""The three cross-owner function signatures, as typing Protocols (docs/SPEC.md).

Owners: `EvaluateFn` is implemented in oracle/ (B), `RunFn` in src/nemogate/proof/ (A) and
`MakeMiddlewareFn` in gate/ (B). Changing any of these needs the other owner's approval.
These are signatures only; there are no implementations here.
"""

from typing import Any, Protocol, runtime_checkable

from nemogate.schemas.enums import Arm, Operator
from nemogate.schemas.ledger import Ledger
from nemogate.schemas.run import FsChange, RunConfig, RunRecord, Violation
from nemogate.schemas.trace import Event


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
    # list[AgentMiddleware]. The runner passes the SAME list as both
    # build_team(middleware=...) and build_team(subagent_middleware=...): planner middleware does
    # not run inside subagents (docs/FACTS.md section 4), and the gate tells the stacks apart by
    # lc_agent_name.
    def __call__(self, run_config: RunConfig) -> list[Any]: ...
