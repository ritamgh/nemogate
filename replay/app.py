"""What the runner needs from the app under test, so replay/ never imports a specific app.

The reference app implements this in `refapp/app.py`. A second Deep Agents app would implement
the same Protocols and run with no change to replay/ (docs/SPEC.md, generality rule).
"""

from collections.abc import Sequence
from typing import Any, Protocol

from langchain.agents.middleware import AgentMiddleware


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
