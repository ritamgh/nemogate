import asyncio
import gc
from asyncio import Lock
from types import SimpleNamespace

import pytest

from replay.sandbox import ShieldedContreeSandbox


class FakeSession:
    """Stands in for a Contree session: a run cancelled midway poisons it, and a run on
    a session that is still executing or poisoned fails like ContreeImageStateError."""

    uuid = "00000000-0000-0000-0000-000000000000"

    def __init__(self, duration: float = 0.3):
        self.duration = duration
        self.executing = False
        self.poisoned = False
        self.commands: list[str] = []
        self.cwds: list[str | None] = []

    async def run(self, *, shell, timeout, disposable, truncate_output_at, cwd=None):
        if self.executing or self.poisoned:
            raise RuntimeError('Image state "x" cannot have state EXECUTING')
        self.commands.append(shell)
        self.cwds.append(cwd)
        self.executing = True
        try:
            await asyncio.sleep(self.duration)
        except asyncio.CancelledError:
            self.poisoned = True
            raise
        self.executing = False
        result = SimpleNamespace(stdout=f"out:{shell}", stderr="err", exit_code=3, truncated=True)
        return SimpleNamespace(result=result)


def make_sandbox(session: FakeSession, cwd: str | None = None) -> ShieldedContreeSandbox:
    sandbox = object.__new__(ShieldedContreeSandbox)  # skip __init__, which wraps a real session
    sandbox._session = session
    sandbox._id = "contree-test"
    sandbox._lock = Lock()
    sandbox._cwd = cwd
    return sandbox


def test_wait_for_cancelling_aexecute_must_not_leave_the_session_executing():
    async def scenario():
        session = FakeSession()
        sandbox = make_sandbox(session)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sandbox.aexecute("slow"), timeout=0.05)
        nxt = await sandbox.aexecute("next")
        return session, nxt

    session, nxt = asyncio.run(scenario())
    assert session.poisoned is False
    assert session.commands == ["slow", "next"]
    assert nxt.output == "out:nexterr"


def test_aexecute_returns_output_exit_code_and_truncated_unchanged():
    async def scenario():
        return await make_sandbox(FakeSession(duration=0)).aexecute("echo", timeout=5)

    resp = asyncio.run(scenario())
    assert (resp.output, resp.exit_code, resp.truncated) == ("out:echoerr", 3, True)


def test_a_command_failing_after_its_caller_was_cancelled_logs_no_unretrieved_exception(caplog):
    class Failing(FakeSession):
        async def run(self, **kw):
            await asyncio.sleep(0.1)
            raise ValueError("boom")

    async def scenario():
        sandbox = make_sandbox(Failing())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sandbox.aexecute("x"), timeout=0.02)
        await asyncio.sleep(0.3)  # let the orphaned command finish and be garbage collected
        gc.collect()

    asyncio.run(scenario())
    assert "never retrieved" not in caplog.text


def test_commands_run_from_the_configured_directory():
    """Catches a command running from the image default directory instead of the repo."""

    async def scenario():
        session = FakeSession(duration=0)
        await make_sandbox(session, cwd="/some/root").aexecute("grep -rn x .")
        await make_sandbox(session).aexecute("pwd")
        return session

    session = asyncio.run(scenario())
    assert session.commands == ["grep -rn x .", "pwd"]
    assert session.cwds == ["/some/root", None]


def test_the_constructor_keeps_the_directory_and_defaults_to_none(monkeypatch):
    """Catches `cwd` being accepted by `__init__` but never reaching `session.run`."""
    # `ContreeSandbox.__init__` wraps any session that is not a `ContreeSession`;
    # patching the class makes the fake one count.
    monkeypatch.setattr("contree_sdk.langchain.sandbox.ContreeSession", FakeSession)

    async def scenario():
        session = FakeSession(duration=0)
        await ShieldedContreeSandbox(session, cwd="/work/tree").aexecute("ls")
        await ShieldedContreeSandbox(session).aexecute("ls")
        return session

    assert asyncio.run(scenario()).cwds == ["/work/tree", None]
