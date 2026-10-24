"""build_team(): the research team (a lead delegating to a researcher that can search the web).

This differs from the LangChain quickstart on purpose: the quickstart builds one deep agent with no
custom subagents. Here the lead cannot search at all and the researcher is an explicit subagent,
so every search crosses the delegation boundary `delegation:lead->researcher` and the handoff the
rule has to survive is guaranteed to happen.

The gate is not attached here. `middleware` goes on the lead (delegation and return boundaries),
`subagent_middleware` on the researcher (its inner tool calls).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from deepagents import create_deep_agent
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from common.llm import get_model
from refapp.research.prompts import (
    LEAD_PROMPT,
    RESEARCHER_DESCRIPTION,
    RESEARCHER_PROMPT,
)
from refapp.team import _general_purpose_disabled
from schemas.enums import Role

REPO = "/repo"
AGENTS = ("lead", "researcher")

# The built-in file tools each agent gets. The lead writes the result into the repo; the researcher
# reads only (Deep Agents requires `read_file` in any allow-list). `execute` is in neither.
LEAD_FILE_TOOLS = ["read_file", "write_file"]
RESEARCHER_FILE_TOOLS = ["read_file"]


def build_team(
    backend: Any,
    *,
    search_tool: BaseTool,
    models: Mapping[str, BaseChatModel] | None = None,
    middleware: Sequence[AgentMiddleware] = (),
    subagent_middleware: Sequence[AgentMiddleware] = (),
    checkpointer: Any = None,
):
    """Build the team as a compiled Deep Agents graph (the lead is the entry point).

    `search_tool` is the researcher's only custom tool. `models` maps an agent name in `AGENTS` to
    its model; default `get_model(Role.sut)` for each.
    """
    unknown = set(models or {}) - set(AGENTS)
    if unknown:
        raise ValueError(f"unknown agents in models: {sorted(unknown)}; expected {AGENTS}")
    chosen = {name: (models or {}).get(name) or get_model(Role.sut) for name in AGENTS}

    def fs(allowed: list[str]) -> FilesystemMiddleware:
        # Same name as the default slot, so it replaces the default (which would add `execute`
        # on a shell backend) in the lead and in the subagent (docs/FACTS.md section 7).
        return FilesystemMiddleware(backend=backend, tools=allowed)

    subagents = [
        {
            "name": "researcher",
            "description": RESEARCHER_DESCRIPTION,
            "system_prompt": RESEARCHER_PROMPT,
            "model": chosen["researcher"],
            "tools": [search_tool],
            "middleware": [fs(RESEARCHER_FILE_TOOLS), *subagent_middleware],
        }
    ]
    with _general_purpose_disabled(chosen["lead"]):
        return create_deep_agent(
            model=chosen["lead"],
            system_prompt=LEAD_PROMPT,
            subagents=subagents,
            middleware=[fs(LEAD_FILE_TOOLS), *middleware],
            backend=backend,
            checkpointer=checkpointer,
        )
