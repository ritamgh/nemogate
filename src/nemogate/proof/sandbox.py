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

    `cwd` is the directory every command runs from. `ContreeSandbox` sets none, so commands
    start in the image's default directory and a path-less `grep` or `glob` searches there,
    not in the repo (FACTS §9).
    """

    def __init__(self, session, *, cwd: str | None = None):
        super().__init__(session)
        self._cwd = cwd

    async def _run(self, command: str, timeout: int | None) -> ExecuteResponse:
        # Mirrors ContreeSandbox.aexecute (contree_sdk/langchain/sandbox.py), plus `cwd`.
        async with self._lock:
            result = (
                await self._session.run(
                    shell=command,
                    timeout=timeout,
                    disposable=False,
                    truncate_output_at=10 * 1024 * 1024,
                    cwd=self._cwd,
                )
            ).result
        output = "".join(str(part) for part in (result.stdout, result.stderr) if part is not None)
        return ExecuteResponse(
            output=output, exit_code=result.exit_code, truncated=result.truncated
        )

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        task = asyncio.ensure_future(self._run(command, timeout))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_retrieve)
            raise
