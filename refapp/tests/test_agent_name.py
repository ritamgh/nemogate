"""Which runtime field tells gate middleware the coder from the reviewer (FACTS section 7)."""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import LocalShellBackend
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_config

from common.llm import get_model
from refapp.team import build_team


class NameProbe(AgentMiddleware):
    """Records, per tool call, the agent name visible at tool-call time, from two places."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[dict[str, Any]] = []

    def _record(self, request: Any) -> None:
        from_context = get_config()["metadata"]
        from_request = request.runtime.config["metadata"]
        self.seen.append(
            {
                "tool": request.tool_call["name"],
                "context_has_key": "lc_agent_name" in from_context,
                "context": from_context.get("lc_agent_name"),
                "request": from_request.get("lc_agent_name"),
            }
        )

    def wrap_tool_call(self, request, handler):
        self._record(request)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        self._record(request)
        return await handler(request)


def call(name: str, call_id: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "id": call_id, "args": args}])


def delegate(subagent: str) -> AIMessage:
    return call("task", f"task_{subagent}", description="Look.", subagent_type=subagent)


@pytest.fixture
def backend(tmp_path: Path) -> LocalShellBackend:
    (tmp_path / "repo").mkdir()
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    return LocalShellBackend(
        root_dir=tmp_path, virtual_mode=True, inherit_env=True, env={"PATH": path}
    )


def run_team(backend, tmp_path, mode: str) -> NameProbe:
    probe = NameProbe()
    models = {
        "planner": get_model(
            "sut",
            stub_responses=[
                delegate("coder"),
                call("ls", "ls_planner", path="/repo"),
                delegate("reviewer"),
                AIMessage(content="Done."),
            ],
        ),
        "coder": get_model(
            "sut",
            stub_responses=[call("ls", "ls_coder", path="/repo"), AIMessage(content="Coded.")],
        ),
        "reviewer": get_model(
            "sut",
            stub_responses=[call("ls", "ls_reviewer", path="/repo"), AIMessage(content="Fine.")],
        ),
    }
    team = build_team(
        backend,
        models=models,
        middleware=[probe],
        subagent_middleware=[probe],
        shell_repo=str(tmp_path / "repo"),
    )
    message = {"messages": [HumanMessage("Look around.")]}
    if mode == "sync":
        team.invoke(message)
    else:
        asyncio.run(team.ainvoke(message))
    return probe


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_tool_calls_carry_the_calling_agents_name_in_config_metadata(backend, tmp_path, mode):
    # Breaks if gate middleware cannot tell the coder from the reviewer: the one pair of
    # middleware instances sits in both subagents, so `lc_agent_name` is the only discriminator.
    # Fails if Deep Agents or LangChain stop naming subagent graphs after their spec name.
    probe = run_team(backend, tmp_path, mode)

    # Order: planner task(coder), coder ls, planner ls, planner task(reviewer), reviewer ls.
    assert [(row["tool"], row["context"]) for row in probe.seen] == [
        ("task", None),
        ("ls", "coder"),
        ("ls", None),
        ("task", None),
        ("ls", "reviewer"),
    ]
    # The runtime handed to the middleware carries the same config as the ambient one.
    assert [row["request"] for row in probe.seen] == [row["context"] for row in probe.seen]
    # The planner's key is present but None: create_deep_agent sets it to its `name`, which
    # build_team does not pass.
    assert all(row["context_has_key"] for row in probe.seen)
