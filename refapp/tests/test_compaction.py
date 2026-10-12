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
BOUNDARY = "delegation:planner->coder"
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


def run_team(backend, planner_script, coder_script, compaction, tmp_path, reviewer_script=None):
    models = {
        "planner": SpyModel(role="sut", responses=planner_script),
        "coder": get_model("sut", stub_responses=coder_script),
        "reviewer": get_model("sut", stub_responses=reviewer_script),
    }

    async def go():
        # The runner's own checkpointer, so the private event is proven to survive its storage.
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as saver:
            team = build_team(backend, models=models, checkpointer=saver, compaction=compaction)
            await team.ainvoke({"messages": [HumanMessage(USER_MESSAGE)]}, CONFIG)
            state = await team.aget_state(CONFIG)
            return state, await compaction_evidence(team, CONFIG, state, BOUNDARY)

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
    # The evidence is the compaction behind the delegation (FIRST), not the later one (SECOND).
    assert "FIRST-SUMMARY" in evidence["summary"]
    assert "SECOND-SUMMARY" not in evidence["summary"]


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


def test_a_compaction_after_the_delegation_is_not_reported(repo_backend, tmp_path):
    # Breaks if a compaction that happened after the planner delegated (it could not have removed
    # the rule from that handoff) is reported as the compaction behind the handoff.
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
    state, evidence = run_team(
        repo_backend, planner, coder, compaction=(8000, 1), tmp_path=tmp_path
    )

    assert state.values["_summarization_event"]["cutoff_index"] > 0  # it did fire, later
    assert "LATE-SUMMARY" in str(state.values["_summarization_event"]["summary_message"].content)
    assert evidence is None
    assert "migrations/" in SEEN[1]


def test_evidence_is_the_last_compaction_before_the_delegation_to_the_target(
    repo_backend, tmp_path
):
    # Breaks if the evidence takes its content from a later compaction than its timing (the final
    # state holds THIRD-SUMMARY), or counts the planner's delegation to the reviewer as the
    # delegation to the coder: the coder's handoff was written after SECOND-SUMMARY, and the
    # first summary predates both.
    to_reviewer = call("task", "c-rev", description="Review.", subagent_type="reviewer")
    planner = [
        call("ls", "p-ls", path="/repo"),
        AIMessage(content="FIRST-SUMMARY"),
        to_reviewer,
        AIMessage(content="SECOND-SUMMARY"),
        DELEGATE,
        AIMessage(content="THIRD-SUMMARY"),
        AIMessage(content="All done."),
    ]
    state, evidence = run_team(
        repo_backend,
        planner,
        CODER,
        compaction=(1, 1),
        tmp_path=tmp_path,
        reviewer_script=[AIMessage(content="Looks fine.")],
    )

    assert "THIRD-SUMMARY" in str(state.values["_summarization_event"]["summary_message"].content)
    assert "SECOND-SUMMARY" in evidence["summary"]
    assert "THIRD-SUMMARY" not in evidence["summary"]
    assert evidence["before_delegation"] is True


def test_a_compaction_before_only_a_delegation_to_another_agent_is_not_reported(
    repo_backend, tmp_path
):
    # Breaks if the evidence treats a delegation to the reviewer as the target delegation: here
    # the planner is compacted before the reviewer call and never delegates to the coder.
    to_reviewer = call("task", "c-rev", description="Review.", subagent_type="reviewer")
    planner = [
        call("ls", "p-ls", path="/repo"),
        AIMessage(content="FIRST-SUMMARY"),
        to_reviewer,
        AIMessage(content="SECOND-SUMMARY"),
        AIMessage(content="All done."),
    ]
    _, evidence = run_team(
        repo_backend,
        planner,
        CODER,
        compaction=(1, 1),
        tmp_path=tmp_path,
        reviewer_script=[AIMessage(content="Looks fine.")],
    )

    assert evidence["before_delegation"] is None
    assert "SECOND-SUMMARY" in evidence["summary"]  # nothing to pin it to: the latest event


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
