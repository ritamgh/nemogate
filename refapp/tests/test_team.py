"""The real team (Deep Agents planner + coder + reviewer) driven by scripted stub models."""

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import LocalShellBackend
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from common.llm import get_model
from refapp.team import AGENTS, REPO, build_team

PLANNER_TOOLS = {
    "ls",
    "read_file",
    "glob",
    "grep",
    "run_tests",
    "request_approval",
    "http_get",
    "task",
}
CODER_TOOLS = {
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "delete",
    "glob",
    "grep",
    "run_tests",
    "request_approval",
    "http_get",
}
REVIEWER_TOOLS = {"ls", "read_file", "glob", "grep", "run_tests"}
TASK_USER_MESSAGE = "Add a hello function in a new file."


class Probe(AgentMiddleware):
    """Records the tools offered on each model call and every tool call, sync and async."""

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label
        self.offered: list[set[str]] = []
        self.task_description = ""
        self.tool_calls: list[dict[str, Any]] = []
        self.results: list[str] = []

    @property
    def name(self) -> str:
        return f"Probe_{self.label}"

    def _model(self, request: Any) -> None:
        self.offered.append({t.name for t in request.tools})
        for tool in request.tools:
            if tool.name == "task":
                self.task_description = tool.description

    def _tool(self, request: Any) -> None:
        self.tool_calls.append(request.tool_call)

    def _result(self, result: Any) -> Any:
        self.results.append(str(getattr(result, "content", result)))
        return result

    def wrap_model_call(self, request, handler):
        self._model(request)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        self._model(request)
        return await handler(request)

    def wrap_tool_call(self, request, handler):
        self._tool(request)
        return self._result(handler(request))

    async def awrap_tool_call(self, request, handler):
        self._tool(request)
        return self._result(await handler(request))


def listed_subagents(task_description: str) -> list[str]:
    """Names in the task tool's 'Available agent types' list (its boilerplate also says
    'general-purpose', so only the listed lines count)."""
    listing = task_description.split("Available agent types")[1].split("Specify subagent_type")[0]
    return re.findall(r"^- ([\w-]+):", listing, flags=re.MULTILINE)


def task_call(subagent: str, description: str = "Do the work described.") -> AIMessage:
    call = {
        "name": "task",
        "id": f"call_task_{subagent}",
        "args": {"description": description, "subagent_type": subagent},
    }
    return AIMessage(content="", tool_calls=[call])


def write_call(path: str, content: str) -> AIMessage:
    call = {
        "name": "write_file",
        "id": "call_write_1",
        "args": {"file_path": path, "content": content},
    }
    return AIMessage(content="", tool_calls=[call])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def backend(tmp_path: Path) -> LocalShellBackend:
    # The agents see /repo/...; virtual_mode lands those paths in tmp_path/repo.
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    return LocalShellBackend(
        root_dir=tmp_path, virtual_mode=True, inherit_env=True, env={"PATH": path}
    )


def scripted_models(planner: list[AIMessage], coder=(), reviewer=()):
    return {
        "planner": get_model("sut", stub_responses=planner),
        "coder": get_model("sut", stub_responses=list(coder)),
        "reviewer": get_model("sut", stub_responses=list(reviewer)),
    }


def delegate_to_coder_script():
    planner = [task_call("coder"), AIMessage(content="The coder finished; all done.")]
    coder = [
        write_call("/repo/x.py", "def hello():\n    return 'hi'\n"),
        AIMessage(content="I created /repo/x.py with hello()."),
    ]
    return scripted_models(planner, coder)


def assert_delegation_run(out, repo, planner_probe, sub_probe):
    messages = out["messages"]
    assert (repo / "x.py").read_text() == "def hello():\n    return 'hi'\n"
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage) and m.name == "task"]
    assert [m.content for m in tool_msgs] == ["I created /repo/x.py with hello()."]
    assert messages[-1].content == "The coder finished; all done."
    # The planner's probe saw the task call; the subagent probe saw write_file, not the reverse.
    assert [c["name"] for c in planner_probe.tool_calls] == ["task"]
    assert [c["name"] for c in sub_probe.tool_calls] == ["write_file"]
    assert sub_probe.tool_calls[0]["args"]["file_path"] == "/repo/x.py"
    # Exactly the README table: no execute anywhere, delete only for the coder.
    assert planner_probe.offered
    assert all(offered == PLANNER_TOOLS for offered in planner_probe.offered)
    assert sub_probe.offered
    assert all(offered == CODER_TOOLS for offered in sub_probe.offered)
    # The planner can delegate to exactly coder and reviewer.
    assert listed_subagents(planner_probe.task_description) == ["coder", "reviewer"]


def test_planner_delegates_to_coder_who_writes_the_file(backend, repo):
    # Breaks if the delegation, the file write through the backend or the return path is miswired.
    planner_probe, sub_probe = Probe("planner"), Probe("sub")
    team = build_team(
        backend,
        models=delegate_to_coder_script(),
        middleware=[planner_probe],
        subagent_middleware=[sub_probe],
        shell_repo=str(repo),
    )

    out = team.invoke({"messages": [HumanMessage(TASK_USER_MESSAGE)]})

    assert_delegation_run(out, repo, planner_probe, sub_probe)


def test_the_same_run_works_under_ainvoke(backend, repo):
    # Breaks if any tool or middleware in the team is sync-only.
    planner_probe, sub_probe = Probe("planner"), Probe("sub")
    team = build_team(
        backend,
        models=delegate_to_coder_script(),
        middleware=[planner_probe],
        subagent_middleware=[sub_probe],
        shell_repo=str(repo),
    )

    out = asyncio.run(team.ainvoke({"messages": [HumanMessage(TASK_USER_MESSAGE)]}))

    assert_delegation_run(out, repo, planner_probe, sub_probe)


def test_reviewer_is_offered_only_read_tools_and_run_tests(backend, repo):
    # Breaks if the reviewer can write, delete, execute or ask for approval.
    sub_probe = Probe("sub")
    models = scripted_models(
        [task_call("reviewer"), AIMessage(content="Review received.")],
        reviewer=[AIMessage(content="Looks fine; no problems found.")],
    )
    team = build_team(backend, models=models, subagent_middleware=[sub_probe], shell_repo=str(repo))

    out = team.invoke({"messages": [HumanMessage(TASK_USER_MESSAGE)]})

    assert sub_probe.offered == [REVIEWER_TOOLS]
    tool_msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_msgs] == ["Looks fine; no problems found."]


def test_coder_run_tests_runs_pytest_in_the_repo_through_the_backend(backend, repo):
    # Breaks if run_tests is not wired to the team's backend or ignores shell_repo.
    (repo / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    run = {"name": "run_tests", "id": "call_rt", "args": {}}
    sub_probe = Probe("sub")
    models = scripted_models(
        [task_call("coder"), AIMessage(content="Done.")],
        [AIMessage(content="", tool_calls=[run]), AIMessage(content="Ran the tests.")],
    )
    team = build_team(backend, models=models, subagent_middleware=[sub_probe], shell_repo=str(repo))

    team.invoke({"messages": [HumanMessage(TASK_USER_MESSAGE)]})

    assert [c["name"] for c in sub_probe.tool_calls] == ["run_tests"]
    assert sub_probe.results[0].startswith("exit code: 0\n")
    assert "1 passed" in sub_probe.results[0]


def test_models_default_to_the_sut_stub_for_every_agent(backend, repo):
    # Breaks if build_team needs models passed in, or builds one without get_model.
    team = build_team(backend, shell_repo=str(repo))

    out = team.invoke({"messages": [HumanMessage(TASK_USER_MESSAGE)]})

    assert out["messages"][-1].content == "stub reply from sut"
    assert AGENTS == ("planner", "coder", "reviewer")
    assert REPO == "/repo"


def test_unknown_model_key_is_rejected(backend, repo):
    with pytest.raises(ValueError, match="auditor"):
        build_team(backend, models={"auditor": get_model("sut")}, shell_repo=str(repo))


def test_checkpointer_is_passed_through(backend, repo):
    # Breaks if build_team drops the checkpointer (forking needs the planner's checkpoints).
    saver = InMemorySaver()
    team = build_team(backend, checkpointer=saver, shell_repo=str(repo))
    config = {"configurable": {"thread_id": "t1"}}

    team.invoke({"messages": [HumanMessage(TASK_USER_MESSAGE)]}, config)

    assert [m.content for m in team.get_state(config).values["messages"]] == [
        TASK_USER_MESSAGE,
        "stub reply from sut",
    ]
