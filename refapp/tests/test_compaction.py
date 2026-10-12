"""The planner-compaction knob: Deep Agents' SummarizationMiddleware on the planner only, driven
by scripted stub models (no network). The summary a stub writes is a scripted reply."""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from deepagents.backends import LocalShellBackend
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import refapp.app
from common.llm import StubChatModel, get_model
from refapp.team import build_team
from replay.carriage import compaction_evidence

RULE = "Heads up: nothing under migrations/ may be touched."
USER_MESSAGE = f"Add a priority field to tasks. {RULE}"
CONFIG = {"configurable": {"thread_id": "t1"}}


class SpyModel(StubChatModel):
    """A stub that keeps the text of the messages each call received."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        SEEN.append(" || ".join(str(m.content) for m in messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


SEEN: list[str] = []


def call(name: str, call_id: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "id": call_id, "args": args}])


@pytest.fixture(autouse=True)
def _clear_seen():
    SEEN.clear()


@pytest.fixture
def repo_backend(tmp_path: Path) -> LocalShellBackend:
    (tmp_path / "repo").mkdir()
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    return LocalShellBackend(
        root_dir=tmp_path, virtual_mode=True, inherit_env=True, env={"PATH": path}
    )


def run_team(backend, planner_script, coder_script, compaction, tmp_path):
    models = {
        "planner": SpyModel(role="sut", responses=planner_script),
        "coder": get_model("sut", stub_responses=coder_script),
        "reviewer": get_model("sut"),
    }

    async def go():
        # The runner's own checkpointer, so the private event is proven to survive its storage.
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as saver:
            team = build_team(backend, models=models, checkpointer=saver, compaction=compaction)
            await team.ainvoke({"messages": [HumanMessage(USER_MESSAGE)]}, CONFIG)
            state = await team.aget_state(CONFIG)
            return state, await compaction_evidence(team, CONFIG, state)

    return asyncio.run(go())


DELEGATE = call("task", "c-task", description="Add the field.", subagent_type="coder")
CODER = [call("ls", "c-ls", path="/repo"), AIMessage(content="Added the field.")]


def test_a_low_trigger_compacts_the_planner_and_it_loses_the_rule(repo_backend, tmp_path):
    # Breaks if the knob does not install SummarizationMiddleware on the planner, or installs it
    # where it never fires: the planner would then see the user's rule when it delegates.
    # Trigger 1 summarizes at every call that has more than `keep` messages: the planner looks
    # (ls), is compacted, delegates, is compacted again, then answers.
    planner = [
        call("ls", "p-ls", path="/repo"),
        AIMessage(content="FIRST-SUMMARY the user wants a priority field."),
        DELEGATE,
        AIMessage(content="SECOND-SUMMARY the field was added."),
        AIMessage(content="All done."),
    ]
    state, evidence = run_team(repo_backend, planner, CODER, compaction=(1, 1), tmp_path=tmp_path)

    # The message that carries the delegation was written after the first compaction: the model
    # saw the summary and no longer the user's words.
    delegating_view = SEEN[2]
    assert "FIRST-SUMMARY" in delegating_view
    assert "migrations/" not in delegating_view
    # The first call, before any compaction, did see the rule.
    assert "migrations/" in SEEN[0]
    # The event is readable from the graph state and the history, and the kept state is intact.
    assert state.values["messages"][0].content == USER_MESSAGE
    assert evidence["cutoff_index"] > 0
    assert evidence["before_delegation"] is True
    assert "SECOND-SUMMARY" in evidence["summary"]  # the latest event wins


def test_without_the_knob_the_planner_keeps_the_rule_and_no_event_is_written(
    repo_backend, tmp_path
):
    # Breaks if an unset knob changes behaviour: the same run must delegate with the full history
    # and write no summarization event.
    planner = [call("ls", "p-ls", path="/repo"), DELEGATE, AIMessage(content="All done.")]
    state, evidence = run_team(repo_backend, planner, CODER, compaction=None, tmp_path=tmp_path)

    assert "migrations/" in SEEN[1]  # the delegating call still has the user's message
    assert not state.values.get("_summarization_event")
    assert evidence is None


def test_compaction_is_installed_on_the_planner_only(repo_backend, tmp_path):
    # Breaks if the knob also compacts the coder: at trigger 1 a coder that makes a second tool
    # call would need a summary reply it was not scripted to give, and the run would raise.
    planner = [
        call("ls", "p-ls", path="/repo"),
        AIMessage(content="FIRST-SUMMARY"),
        DELEGATE,
        AIMessage(content="SECOND-SUMMARY"),
        AIMessage(content="All done."),
    ]
    state, _ = run_team(repo_backend, planner, CODER, compaction=(1, 1), tmp_path=tmp_path)
    assert state.values["messages"][-1].content == "All done."


def test_a_compaction_after_the_delegation_is_not_before_it(repo_backend, tmp_path):
    # Breaks if before_delegation cannot tell a compaction that happened after the planner
    # delegated (it could not have removed the rule from that handoff) from one before it.
    # The coder's 60000-character report is about 15000 tokens: far above the 8000 trigger, while
    # the planner's context before it (system prompt, tools, a few short messages) is far below.
    big_report = "x" * 60000
    coder = [call("ls", "c-ls", path="/repo"), AIMessage(content=big_report)]
    planner = [
        call("ls", "p-ls", path="/repo"),
        DELEGATE,
        AIMessage(content="LATE-SUMMARY"),
        AIMessage(content="All done."),
    ]
    _, evidence = run_team(repo_backend, planner, coder, compaction=(8000, 1), tmp_path=tmp_path)

    assert evidence["before_delegation"] is False
    assert "LATE-SUMMARY" in evidence["summary"]
    assert "migrations/" in SEEN[1]


def test_the_app_installs_compaction_from_the_environment(monkeypatch):
    # Breaks if RefApp.build ignores the env knob, so a live run labelled as compaction would
    # run uncompacted.
    seen = []
    monkeypatch.setattr(refapp.app, "build_team", lambda backend, **kw: seen.append(kw))
    monkeypatch.setenv("NEMOGATE_COMPACT_TOKENS", "7000")
    monkeypatch.setenv("NEMOGATE_COMPACT_KEEP", "3")
    refapp.app.APP.build(object(), middleware=[], subagent_middleware=[], checkpointer=None)
    monkeypatch.delenv("NEMOGATE_COMPACT_TOKENS")
    monkeypatch.delenv("NEMOGATE_COMPACT_KEEP")
    refapp.app.APP.build(object(), middleware=[], subagent_middleware=[], checkpointer=None)

    assert [kw["compaction"] for kw in seen] == [(7000, 3), None]
