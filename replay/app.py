"""What the runner needs from the app under test, so replay/ never imports a specific app.

The reference app implements this in `refapp/app.py`. A second Deep Agents app would implement
the same Protocols and run with no change to replay/ (docs/SPEC.md, generality rule).
"""

from collections.abc import Sequence
from typing import Any, Protocol

from langchain.agents.middleware import AgentMiddleware

from schemas.enums import Arm, Operator


class AppTask(Protocol):
    """One task: the user message that states the rule, and which rule and boundary it targets."""

    @property
    def id(self) -> str: ...
    @property
    def constraint(self) -> str: ...  # the run's target constraint, e.g. a ledger entry id
    @property
    def boundary(self) -> str: ...  # the boundary the rule must cross
    @property
    def user_message(self) -> str: ...

    # Optional: `refusal(arm, operator) -> str | None`. A task that cannot be measured under some
    # (arm, operator) cells returns why; the runner then refuses those cells before any run.


class App(Protocol):
    @property
    def repo_root(self) -> str:
        """Absolute path of the repo inside the sandbox; the filesystem diff is relative to it."""
        ...

    def tasks(self) -> Sequence[AppTask]:
        """Every task the app offers, sorted by id."""
        ...

    async def base_image(self, client: Any) -> str:
        """The Contree image uuid every run of this app starts from."""
        ...

    def build(
        self,
        backend: Any,
        *,
        middleware: Sequence[AgentMiddleware],
        subagent_middleware: Sequence[AgentMiddleware],
        checkpointer: Any,
    ) -> Any:
        """The compiled agent graph (`ainvoke`, `aget_state`) on `backend`.

        `middleware` goes on the entry agent, `subagent_middleware` on every subagent.
        """
        ...

    async def task_success(self, session: Any, task: AppTask) -> tuple[bool, str]:
        """Did the run get the task done, judged in `session`'s current image without changing
        it. The str is the tail of the failing output, or ""."""
        ...


def pairs(arms: Sequence[Arm], operators: Sequence[Operator]) -> list[tuple[Arm, Operator]]:
    """Every (arm, operator) to run: natural only unmutated, mutated only mutated, and the
    blocking arms (block_only, gated) with any operator, `none` included: the gate acting on a
    run whose handoff nothing sabotaged.

    Arms outer, both in the order given.
    """

    def runs(arm: Arm, operator: Operator) -> bool:
        if arm == Arm.natural:
            return operator == Operator.none
        if arm == Arm.mutated:
            return operator != Operator.none
        return True

    return [(a, o) for a in arms for o in operators if runs(a, o)]
