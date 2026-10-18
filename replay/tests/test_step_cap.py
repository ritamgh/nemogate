"""The step bound on every agent: a subagent that never stops ends its run, not the batch.

Deep Agents compiles each subagent with its own `recursion_limit` of 9999, which wins over the
one the runner passes the planner (docs/FACTS.md, "Subagent step bound"). These tests run a REAL
Deep Agents graph (planner delegating to a subagent whose stub model calls a tool forever) so the
bound is checked where it failed, not against a fake."""

import asyncio
import json

import pytest
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from common.llm import StubChatModel, get_model
from replay.runner import SUBAGENT_STEP_LIMIT, Runner
from replay.step_cap import StepCap, StepLimitReached
from replay.tests.test_runner import FakeApp, Harness
from schemas.enums import Arm, Operator

HARD_STOP = 300  # model calls: far above any limit the tests set, so a miss fails, never hangs
LIMIT = 40  # supersteps; the looping subagent makes two per model call


class RanAway(RuntimeError):
    """What the looping stub raises at HARD_STOP: the bound did not hold."""


class LoopingModel(StubChatModel):
    """Calls the `ping` tool on every turn, forever (until HARD_STOP)."""

    calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        object.__setattr__(self, "calls", self.calls + 1)
        if self.calls >= HARD_STOP:
            raise RanAway(f"reached {HARD_STOP} model calls: no step bound stopped the subagent")
        call = {"name": "ping", "id": f"ping-{self.calls}", "args": {}}
        reply = AIMessage(
            content="",
            tool_calls=[call],
            usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        return ChatResult(generations=[ChatGeneration(message=reply)])


@tool
def ping() -> str:
    """Do nothing."""
    return "pong"


def delegating_graph(subagent_model, checkpointer):
    delegation = {
        "name": "task",
        "id": "t1",
        "args": {"description": "work", "subagent_type": "worker"},
    }
    planner = get_model(
        "sut", stub_responses=[AIMessage(content="", tool_calls=[delegation]), "all done"]
    )
    worker = {
        "name": "worker",
        "description": "Does the work.",
        "system_prompt": "Work.",
        "model": subagent_model,
        "tools": [ping],
    }
    return create_deep_agent(
        model=planner, subagents=[worker], checkpointer=checkpointer, tools=[ping]
    )


class LoopingApp(FakeApp):
    """The first build is the real graph with the runaway subagent; later builds are FakeGraph."""

    def __init__(self):
        super().__init__()
        self.looper = LoopingModel(role="sut")

    def build(self, backend, *, middleware, subagent_middleware, checkpointer):
        if self.builds == 0:
            self.builds += 1
            return delegating_graph(self.looper, checkpointer)
        return super().build(
            backend,
            middleware=middleware,
            subagent_middleware=subagent_middleware,
            checkpointer=checkpointer,
        )


def test_a_subagent_that_never_stops_is_cut_at_the_limit():
    # Breaks if the handler is not inherited by the subagent graph, or does not read the
    # superstep of the graph the model call runs in: the stub would reach HARD_STOP instead.
    model = LoopingModel(role="sut")
    graph = delegating_graph(model, None)
    config = {"recursion_limit": 60, "callbacks": [StepCap(LIMIT)]}

    with pytest.raises(StepLimitReached) as caught:
        asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "go"}]}, config))

    # supersteps 2, 4, ..., LIMIT are allowed: LIMIT / 2 model calls; the next one is refused
    assert model.calls == 20
    assert str(caught.value) == (
        "Step limit of 40 reached by an agent: its model call at step 42 was not made. "
        "The run is ended and recorded as an error."
    )


def test_without_the_handler_the_planners_recursion_limit_does_not_reach_the_subagent():
    # Pins the fact the fix rests on (FACTS: Subagent step bound). If a Deep Agents upgrade makes
    # the parent's limit reach the subagent, this fails and the handler may be unnecessary.
    model = LoopingModel(role="sut")
    graph = delegating_graph(model, None)

    with pytest.raises(RanAway):
        asyncio.run(
            graph.ainvoke(
                {"messages": [{"role": "user", "content": "go"}]}, {"recursion_limit": 60}
            )
        )

    assert model.calls == HARD_STOP


def test_a_run_that_stays_within_the_limit_is_not_cut():
    # Breaks if the cap counts across delegations, or cuts the planner's own short loop.
    planner_graph = delegating_graph(get_model("sut", stub_responses=["first", "second"] * 3), None)
    config = {"recursion_limit": 60, "callbacks": [StepCap(LIMIT)]}

    out = asyncio.run(
        planner_graph.ainvoke({"messages": [{"role": "user", "content": "go"}]}, config)
    )

    assert out["messages"][-1].content == "all done"


def test_the_runner_records_the_runaway_run_as_an_error_and_goes_on(tmp_path):
    # Breaks if the cap's exception escapes `arun`, leaves the run without error.json, or stops
    # the batch before the next run.
    h = Harness(tmp_path, subagent_step_limit=LIMIT)
    h.app = h.runner.app = looping = LoopingApp()

    records = asyncio.run(
        h.runner.arun(["X1", "X2"], [Arm.gated], [Operator.drop], 1, repo_ref="HEAD")
    )

    assert looping.looper.calls == 20
    assert [r.task_id for r in records] == ["X2"]  # the next run finished
    (error,) = h.runner.errors
    assert (error.task_id, error.error_type) == ("X1", "replay.step_cap.StepLimitReached")
    written = json.loads((h.runs_dir / error.run_id / "error.json").read_text())
    assert written["error_type"] == "replay.step_cap.StepLimitReached"
    assert written["message"].startswith("Step limit of 40 reached by an agent")


def test_the_default_limit_is_above_what_the_longest_normal_run_needed():
    # The longest single delegation in the Oct 19 sweep's finished runs made 88 tool calls: about
    # 178 supersteps. Breaks if the default is lowered into that range.
    assert SUBAGENT_STEP_LIMIT >= 200
    assert Runner.__init__.__kwdefaults__["subagent_step_limit"] == SUBAGENT_STEP_LIMIT


def test_the_reference_teams_coder_is_cut_too(tmp_path):
    # The failing case: the reference team's coder, built with `subagent_middleware` as the
    # runner builds it, looping on write_file. Breaks if the cap stops reaching a team built by
    # `build_team`, or if its subagent graph gets a limit the callbacks cannot see.
    from deepagents.backends import LocalShellBackend
    from langgraph.checkpoint.memory import InMemorySaver

    from refapp.team import build_team

    class WriteForever(LoopingModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop, run_manager, **kwargs)
            call = result.generations[0].message.tool_calls[0]
            call.update(name="write_file", args={"file_path": "/repo/report.txt", "content": "x"})
            return result

    coder = WriteForever(role="sut")
    delegation = {
        "name": "task",
        "id": "t1",
        "args": {"description": "work", "subagent_type": "coder"},
    }
    planner = get_model("sut", stub_responses=[AIMessage(content="", tool_calls=[delegation])])
    (tmp_path / "repo").mkdir()
    team = build_team(
        LocalShellBackend(root_dir=tmp_path, virtual_mode=True, inherit_env=True),
        models={"planner": planner, "coder": coder, "reviewer": get_model("sut")},
        checkpointer=InMemorySaver(),
        shell_repo=str(tmp_path / "repo"),
    )
    config = {
        "configurable": {"thread_id": "t"},
        "recursion_limit": 60,
        "callbacks": [StepCap(LIMIT)],
    }

    with pytest.raises(StepLimitReached):
        asyncio.run(team.ainvoke({"messages": [{"role": "user", "content": "go"}]}, config))

    assert coder.calls == 20
