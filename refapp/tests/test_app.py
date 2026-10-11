"""RefApp implements replay.app.App; the runner's single shared middleware list works on it."""

import asyncio
import inspect
import os
import sys
from pathlib import Path
from typing import Any

from deepagents.backends import LocalShellBackend
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_config

import refapp.sandbox
import refapp.team
from common.llm import get_model
from refapp.app import APP, RefApp
from refapp.tasks import load_tasks
from replay.app import App


def accepts_an_app(app: App) -> App:
    return app


def test_app_has_every_protocol_member_with_the_same_sync_or_async_kind():
    # Breaks if RefApp drops a member or turns an async one sync (the runner awaits base_image
    # and task_success, and calls tasks and build directly).
    assert accepts_an_app(APP) is APP
    assert isinstance(APP, RefApp)
    for name in ("tasks", "build"):
        assert not inspect.iscoroutinefunction(getattr(APP, name))
    for name in ("base_image", "task_success"):
        assert inspect.iscoroutinefunction(getattr(APP, name))
    assert APP.repo_root == "/repo"


def test_tasks_are_the_loaded_tasks():
    assert [t.id for t in APP.tasks()] == [t.id for t in load_tasks()]
    assert [t.id for t in APP.tasks()][:2] == ["T1", "T2"]


def test_base_image_delegates_to_seed_image(monkeypatch):
    seen = []

    async def fake_seed_image(client):
        seen.append(client)
        return "11111111-2222-3333-4444-555555555555"

    monkeypatch.setattr(refapp.sandbox, "seed_image", fake_seed_image)
    client = object()
    assert asyncio.run(APP.base_image(client)) == "11111111-2222-3333-4444-555555555555"
    assert seen == [client]


def test_task_success_passes_the_check_name_and_bytes_of_the_task(monkeypatch):
    seen = []

    async def fake_task_success(session, check_name, check_bytes):
        seen.append((session, check_name, check_bytes))
        return False, "boom"

    monkeypatch.setattr(refapp.sandbox, "task_success", fake_task_success)
    task = load_tasks()[0]
    session = object()
    assert asyncio.run(APP.task_success(session, task)) == (False, "boom")
    assert seen == [(session, "check_t1.py", task.check.read_bytes())]
    assert b"import" in seen[0][2]


class Recorder(AgentMiddleware):
    """One instance records (tool, lc_agent_name) for every tool call it wraps."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, Any]] = []

    async def awrap_tool_call(self, request, handler):
        self.seen.append((request.tool_call["name"], get_config()["metadata"].get("lc_agent_name")))
        return await handler(request)


def call(name: str, call_id: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "id": call_id, "args": args}])


def test_one_middleware_instance_shared_by_planner_and_subagents_keeps_agent_identity(
    monkeypatch, tmp_path
):
    # Breaks if the runner's single middleware list (one instance as both `middleware` and
    # `subagent_middleware`) is rejected as a duplicate by Deep Agents/LangChain, or if the
    # instance can no longer tell the planner (None) from the coder ("coder").
    scripts = iter(
        [
            [
                call("task", "t1", description="Look.", subagent_type="coder"),
                call("ls", "ls_planner", path="/repo"),
                AIMessage(content="Done."),
            ],
            [call("ls", "ls_coder", path="/repo"), AIMessage(content="Coded.")],
            [AIMessage(content="Fine.")],
        ]
    )
    # build_team asks get_model(Role.sut) once per agent, in the order planner, coder, reviewer.
    monkeypatch.setattr(
        refapp.team, "get_model", lambda role: get_model(role, stub_responses=next(scripts))
    )
    (tmp_path / "repo").mkdir()
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    backend = LocalShellBackend(
        root_dir=tmp_path, virtual_mode=True, inherit_env=True, env={"PATH": path}
    )
    recorder = Recorder()
    shared = [recorder]

    team = APP.build(backend, middleware=shared, subagent_middleware=shared, checkpointer=None)
    asyncio.run(team.ainvoke({"messages": [HumanMessage("Look around.")]}))

    assert recorder.seen == [("task", None), ("ls", "coder"), ("ls", None)]
