"""A rule across three hops of real Deep Agents graphs: main -> agent_a -> agent_b -> agent_c.

Scripted chat models only, no refapp. Each hop is a real `task` delegation; agent_a and agent_b
are deep agents of their own (CompiledSubAgent), agent_c is a plain SubAgent spec. What each agent
received as its first message is what the delegating agent's `task` call was rewritten into."""

import json

from deepagents import create_deep_agent
from deepagents.middleware.subagents import CompiledSubAgent, SubAgent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from gate.guard import Guard

RULES = """\
version: 1
rules:
  - id: no-shell
    text: Do not run shell commands.
    check:
      tool_forbidden: {name: run_shell}
  - id: no-migrations
    text: Never touch the migrations folder.
    check:
      path_forbidden: {glob: "migrations/**", ops: [write, delete]}
"""
SHELL_OBSERVED = RULES.replace(
    "    text: Do not run shell commands.\n",
    "    text: Do not run shell commands.\n    mode: observe\n",
)
PIN = (
    "\n\nRules from the user that apply to this work:\n"
    "- Do not run shell commands.\n"
    "- Never touch the migrations folder."
)
PIN_MIGRATIONS_ONLY = (
    "\n\nRules from the user that apply to this work:\n- Never touch the migrations folder."
)
BLOCK_SHELL = "Blocked by rule no-shell: Do not run shell commands."

# What each delegator says, in its own words: none of them repeats a rule.
TO_A = "A-task: clean up the build scripts."
TO_B = "B-task: please tidy whatever is left over."
TO_C = "C-task: finish up and free some disk space."


def delegate(sub, description):
    return "task", {"subagent_type": sub, "description": description}


SHELL = ("run_shell", {"cmd": "rm -rf build"})


class ScriptedChat(BaseChatModel):
    """Replies with the next scripted tool call of the agent whose task text it is working on
    (its first human message starts with the script's key), then with plain text. Records, per
    key, the first human message it received and the tool results it saw."""

    scripts: dict
    first_message: dict
    tool_results: dict

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        first = str(next(m for m in messages if isinstance(m, HumanMessage)).content)
        key = next(k for k in self.scripts if first.startswith(k))
        self.first_message[key] = first
        results = [str(m.content) for m in messages if isinstance(m, ToolMessage)]
        self.tool_results[key] = results
        if len(results) < len(self.scripts[key]):
            name, args = self.scripts[key][len(results)]
            reply = AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": f"call-{key}-{len(results)}"}],
            )
        else:
            reply = AIMessage(content=f"{key} finished")
        return ChatResult(generations=[ChatGeneration(message=reply)])


class Chain:
    """The three-hop app. `ran` counts how often each agent's run_shell really executed."""

    def __init__(self, guard, *, guard_b=True, b_runs_shell=False):
        self.ran = {"agent_a": [], "agent_b": [], "agent_c": []}
        scripts = {
            "start": [delegate("agent_a", TO_A)],
            "A-task": [delegate("agent_b", TO_B)],
            "B-task": [*([SHELL] if b_runs_shell else []), delegate("agent_c", TO_C)],
            "C-task": [SHELL],
        }
        model = ScriptedChat(scripts=scripts, first_message={}, tool_results={})
        self.first_message, self.tool_results = model.first_message, model.tool_results
        agent_c = SubAgent(
            name="agent_c",
            description="third hop",
            system_prompt="You are agent_c.",
            model=model,
            tools=[self._shell("agent_c")],
            middleware=guard.middleware(),
        )
        agent_b = create_deep_agent(
            model=model,
            name="agent_b",
            tools=[self._shell("agent_b")],
            middleware=guard.middleware() if guard_b else [],
            subagents=[agent_c],
        )
        agent_a = create_deep_agent(
            model=model,
            name="agent_a",
            tools=[self._shell("agent_a")],
            middleware=guard.middleware(),
            subagents=[CompiledSubAgent(name="agent_b", description="second", runnable=agent_b)],
        )
        self.agent = create_deep_agent(
            model=model,
            name="main",
            middleware=guard.middleware(),
            subagents=[CompiledSubAgent(name="agent_a", description="first", runnable=agent_a)],
        )

    def _shell(self, label):
        @tool("run_shell")
        def run_shell(cmd: str) -> str:
            """Run a shell command."""
            self.ran[label].append(cmd)
            return "ran"

        return run_shell

    def invoke(self):
        return self.agent.invoke(
            {"messages": [("user", "start")]}, {"configurable": {"thread_id": "three-hop"}}
        )


def guard_for(tmp_path, rules=RULES, **kwargs):
    path = tmp_path / "rules.yaml"
    path.write_text(rules, encoding="utf-8")
    return Guard.from_yaml(path, **kwargs)


# Breaks if the re-pin is only applied to the first delegation (or only by the main agent's
# instance): each hop's delegator is a different agent with its own middleware instance.
def test_every_delegation_carries_the_rule_text_at_each_of_the_three_hops(tmp_path):
    chain = Chain(guard_for(tmp_path))
    chain.invoke()
    assert chain.first_message == {
        "start": "start",
        "A-task": TO_A + PIN,
        "B-task": TO_B + PIN,
        "C-task": TO_C + PIN,
    }


# Breaks if the pin is built from what the delegator wrote instead of being appended after it:
# every delegator above restated its task in other words and none of them repeated a rule.
def test_a_delegator_that_restates_the_task_does_not_remove_the_pinned_rules(tmp_path):
    chain = Chain(guard_for(tmp_path))
    chain.invoke()
    sent = [TO_A, TO_B, TO_C]
    assert [d for d in sent if "shell" in d or "migrations" in d] == []
    received = [chain.first_message[k] for k in ("A-task", "B-task", "C-task")]
    assert [d.count("Do not run shell commands.") for d in received] == [1, 1, 1]
    assert [d.count("Never touch the migrations folder.") for d in received] == [1, 1, 1]


# Breaks if the block moves after the call, or the block message loses the rule id: the final
# agent of the chain makes the forbidden call.
def test_the_forbidden_call_of_the_third_hop_is_blocked_and_names_the_rule(tmp_path):
    chain = Chain(guard_for(tmp_path))
    out = chain.invoke()
    assert chain.ran == {"agent_a": [], "agent_b": [], "agent_c": []}
    assert chain.tool_results["C-task"] == [BLOCK_SHELL]
    # the chain ends cleanly: each reply travelled back up as the task result
    assert out["messages"][-1].content == "start finished"


# Breaks if the guard is not read per agent: with no guard in agent_b's spec, nothing re-pins
# agent_b's delegation to agent_c and nothing checks agent_b's own calls. The limit is per hop:
# the guarded hops before and after it still work.
def test_without_the_guard_in_agent_b_that_hop_is_unprotected(tmp_path):
    chain = Chain(guard_for(tmp_path), guard_b=False, b_runs_shell=True)
    chain.invoke()
    assert chain.first_message["A-task"] == TO_A + PIN  # main's guard
    assert chain.first_message["B-task"] == TO_B + PIN  # agent_a's guard
    assert chain.first_message["C-task"] == TO_C  # agent_b has none: not re-pinned
    assert chain.ran["agent_b"] == ["rm -rf build"]  # its own forbidden call goes through
    assert chain.tool_results["B-task"][0] == "ran"
    # agent_c has its own guard, so its call is still blocked, just without the rule in its task
    assert chain.ran["agent_c"] == []
    assert chain.tool_results["C-task"] == [BLOCK_SHELL]


def test_with_the_guard_in_agent_b_the_same_script_is_protected_at_that_hop(tmp_path):
    chain = Chain(guard_for(tmp_path), guard_b=True, b_runs_shell=True)
    chain.invoke()
    assert chain.first_message["C-task"] == TO_C + PIN
    assert chain.ran == {"agent_a": [], "agent_b": [], "agent_c": []}
    assert chain.tool_results["B-task"][0] == BLOCK_SHELL


# Breaks if observe-mode rules are pinned like block ones: the shell rule is only observed, so
# only the block rule's text may be sent on, at every hop.
def test_an_observe_rule_is_not_re_pinned_while_the_block_rule_is(tmp_path):
    chain = Chain(guard_for(tmp_path, SHELL_OBSERVED))
    chain.invoke()
    assert chain.first_message == {
        "start": "start",
        "A-task": TO_A + PIN_MIGRATIONS_ONLY,
        "B-task": TO_B + PIN_MIGRATIONS_ONLY,
        "C-task": TO_C + PIN_MIGRATIONS_ONLY,
    }


# Breaks if an observe rule turns into a block, or is dropped from the trace: the call runs and
# the would-block line names the rule.
def test_an_observed_rule_lets_the_call_run_and_is_traced(tmp_path):
    trace = tmp_path / "trace.jsonl"
    only_observed = SHELL_OBSERVED.split("  - id: no-migrations")[0]
    chain = Chain(guard_for(tmp_path, only_observed, trace_path=trace))
    chain.invoke()
    assert chain.first_message == {
        "start": "start",
        "A-task": TO_A,
        "B-task": TO_B,
        "C-task": TO_C,
    }
    assert chain.ran["agent_c"] == ["rm -rf build"]
    assert chain.tool_results["C-task"] == ["ran"]
    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [
        (e["type"], e["agent"], e["constraint_id"]) for e in lines if e["type"] == "gate_block"
    ] == [("gate_block", "agent_c", "no-shell")]
