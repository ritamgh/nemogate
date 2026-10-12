"""The Contree sandbox the agents run in, hardened against cancelled commands."""

import asyncio

from contree_sdk.langchain.sandbox import ContreeSandbox
from deepagents.backends.protocol import ExecuteResponse


def _retrieve(task: asyncio.Future) -> None:
    # Reading the outcome stops asyncio logging "exception was never retrieved" for a
    # command whose caller has already gone.
    if not task.cancelled():
        task.exception()


class ShieldedContreeSandbox(ContreeSandbox):
    """`ContreeSandbox` whose commands survive the caller being cancelled.

    deepagents wraps some tools (grep, glob) in `asyncio.wait_for`. When it fires, the
    cancellation lands inside `session.run`, which leaves the Contree session
    non-terminal, and the next command fails with `ContreeImageStateError` (FACTS §9).
    Here the command runs as its own task, so it finishes and keeps holding the lock; the
    next command waits for it.
    """

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        task = asyncio.ensure_future(ContreeSandbox.aexecute(self, command, timeout=timeout))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_retrieve)
            raise
