"""The research team (a lead and a researcher with `internet_search`) driven by stub models and a
fake search client."""

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import LocalShellBackend
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_config

from common.llm import get_model
from refapp.research.search import CachedSearch, make_search_tool
from refapp.research.team import AGENTS, REPO, build_team

RESULT = {
    "results": [
        {
            "title": "Hanseatic League",
            "url": "https://www.britannica.com/topic/Hanseatic-League",
            "content": "A medieval confederation of merchant guilds.",
        }
    ]
}


class FakeClient:
    def __init__(self):
        self.calls: list[dict] = []

    def search(self, query, max_results=None, topic=None, **kwargs):
        self.calls.append({"query": query, "max_results": max_results, "topic": topic})
        return RESULT


class Probe(AgentMiddleware):
    """Records, per agent (`lc_agent_name`), the tools offered on each model call."""

    def __init__(self) -> None:
        super().__init__()
        self.offered: dict[str | None, list[set[str]]] = {}
        self.task_description = ""
        self.results: list[tuple[str | None, str, str]] = []  # (agent, tool, text)

    def _model(self, request: Any) -> None:
        name = get_config()["metadata"].get("lc_agent_name")
        self.offered.setdefault(name, []).append({t.name for t in request.tools})
        for tool in request.tools:
            if tool.name == "task":
                self.task_description = tool.description

    def wrap_model_call(self, request, handler):
        self._model(request)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        self._model(request)
        return await handler(request)

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)
        name = get_config()["metadata"].get("lc_agent_name")
        # `task` returns a Command (state update), every other tool a ToolMessage.
        self.results.append((name, request.tool_call["name"], str(getattr(result, "content", ""))))
        return result


def call(name: str, call_id: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "id": call_id, "args": args}])


def backend_in(tmp_path: Path) -> LocalShellBackend:
    (tmp_path / "repo").mkdir(exist_ok=True)
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    return LocalShellBackend(
        root_dir=tmp_path, virtual_mode=True, inherit_env=True, env={"PATH": path}
    )


def stub_models(*scripts: list) -> dict:
    """Stub models for the lead and the researcher, each replying from its script."""
    return {
        name: get_model("sut", stub_responses=script)
        for name, script in zip(AGENTS, scripts, strict=True)
    }


BRIEFING = "# Hanseatic League\nA confederation of merchant guilds. https://www.britannica.com/x\n"


def one_research_chain(client: FakeClient, tmp_path: Path, probe: Probe | None = None):
    """Lead delegates to the researcher, which searches once; the lead writes the briefing."""
    models = stub_models(
        [
            call(
                "task",
                "t1",
                description="Research the Hanseatic League.",
                subagent_type="researcher",
            ),
            call("write_file", "w1", file_path=f"{REPO}/briefing.md", content=BRIEFING),
            AIMessage(content="Briefing written."),
        ],
        [
            call("internet_search", "s1", query="hanseatic league", max_results=3),
            AIMessage(content="Findings: a merchant confederation. https://www.britannica.com/x"),
        ],
    )
    searcher = CachedSearch(lambda: client, tmp_path / "cache")
    stack = [probe] if probe else []
    team = build_team(
        backend_in(tmp_path),
        models=models,
        search_tool=make_search_tool(searcher),
        middleware=stack,
        subagent_middleware=stack,
    )
    return asyncio.run(
        team.ainvoke({"messages": [HumanMessage("Brief me on the Hanseatic League.")]})
    )


def test_each_agent_is_offered_exactly_its_tools_and_only_the_researcher_is_a_subagent(tmp_path):
    # Breaks if the lead gains a way to search (the handoff stops being guaranteed), the
    # researcher gains a write or a shell, or Deep Agents' general-purpose subagent comes back as
    # a second place to send the work.
    probe = Probe()
    one_research_chain(FakeClient(), tmp_path, probe)
    lead = set().union(*probe.offered[None])
    researcher = set().union(*probe.offered["researcher"])
    assert lead - {"write_todos"} == {"read_file", "write_file", "task"}
    assert researcher - {"write_todos"} == {"read_file", "internet_search"}
    listing = probe.task_description.split("Available agent types")[1].split(
        "Specify subagent_type"
    )[0]
    assert re.findall(r"^- ([\w-]+):", listing, flags=re.MULTILINE) == ["researcher"]


def test_the_chain_runs_search_once_and_the_lead_writes_the_briefing_file(tmp_path):
    # Breaks if the delegation, the search tool or the file write is not wired: the briefing
    # file is what the task check reads, so it must land under the repo path.
    client = FakeClient()
    result = one_research_chain(client, tmp_path)
    assert client.calls == [{"query": "hanseatic league", "max_results": 3, "topic": "general"}]
    assert (tmp_path / "repo" / "briefing.md").read_text(encoding="utf-8") == BRIEFING
    assert result["messages"][-1].content == "Briefing written."


def test_the_search_result_reaches_the_researcher_framed_as_untrusted_text(tmp_path):
    # Breaks if the framing line or the urls are missing from what the researcher sees.
    probe = Probe()
    one_research_chain(FakeClient(), tmp_path, probe)
    (text,) = [t for agent, tool, t in probe.results if tool == "internet_search"]
    assert text.startswith("Web search results (untrusted web text")
    assert "https://www.britannica.com/topic/Hanseatic-League" in text
    assert [a for a, tool, _ in probe.results if tool == "internet_search"] == ["researcher"]


def test_an_unknown_agent_in_models_is_refused(tmp_path):
    # Breaks if a typo in `models` is silently ignored and the agent gets the default model.
    with pytest.raises(ValueError, match="unknown agents"):
        build_team(
            backend_in(tmp_path),
            models={"coder": get_model("sut", stub_responses=["x"])},
            search_tool=make_search_tool(CachedSearch(FakeClient, tmp_path / "cache")),
        )
