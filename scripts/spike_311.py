"""Spike for the drop-in rule guard: Q2, Q3 and Q4 of scripts/spike_311.sh (Q1 is the shell part).

A minimal Deep Agents app that uses none of nemogate's refapp, a scripted chat model (no network,
no key), and a recording AgentMiddleware. It prints one line per question, "PASS: ..." or
"FAIL: ...", and with SPIKE_VERBOSE=1 the raw observations on stderr.

    uv run python scripts/spike_311.py
"""

import os
import socket
import sys
from collections import defaultdict
from typing import Any

# Offline proof: no key in the environment, tracing off, and every socket connect counted.
for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
    os.environ.pop(name, None)
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

CONNECTS: list[Any] = []
_real_connect = socket.socket.connect


def _counting_connect(self: socket.socket, address: Any) -> Any:
    CONNECTS.append(address)
    raise OSError("spike_311: network is off")


socket.socket.connect = _counting_connect  # type: ignore[method-assign]

from deepagents import create_deep_agent  # noqa: E402
from deepagents.middleware.subagents import CompiledSubAgent, SubAgent  # noqa: E402
from langchain.agents.middleware import AgentMiddleware  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.config import get_config  # noqa: E402

VERBOSE = os.environ.get("SPIKE_VERBOSE") == "1"
THREAD = "thread-spike-311"


def note(text: str) -> None:
    if VERBOSE:
        print(text, file=sys.stderr)


@tool
def write_note(path: str, text: str) -> str:
    """Write a note (the outside app's own tool, not Deep Agents' write_file)."""
    return f"wrote {path} ({len(text)} chars)"


def _call(tool_name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": call_id}])


def _task(sub: str, text: str) -> tuple[str, dict]:
    return "task", {"subagent_type": sub, "description": text}


def _write(path: str) -> tuple[str, dict]:
    return "write_file", {"file_path": path, "content": f"content of {path}"}


# First human message (the agent's own task text) -> its scripted tool calls, in order. After the
# last one the agent answers in plain text. The task text is how one model class serves every
# agent of the app, because a delegation's description is the subagent's first human message.
SCRIPTS: dict[str, list[tuple[str, dict]]] = {
    "start-A": [
        _write("/main.txt"),
        ("write_note", {"path": "n.txt", "text": "hello"}),
        _task("general-purpose", "DO:gp"),
        _task("worker", "DO:worker"),
        _task("plain", "DO:plain"),
    ],
    "DO:gp": [_write("/gp.txt")],
    "DO:worker": [_write("/worker.txt")],
    "DO:plain": [_write("/plain.txt")],
    "start-B": [_task("mid", "DO:mid")],
    "DO:mid": [_task("leaf", "DO:leaf")],
    "DO:leaf": [_write("/leaf.txt")],
    "start-C": [_task("general-purpose", "DO:gp-override")],
    "DO:gp-override": [_write("/gp-override.txt")],
}


class ScriptedChat(BaseChatModel):
    """Replies with the next scripted tool call, chosen by the first human message and by how
    many tool results the conversation already holds; then with plain text."""

    @property
    def _llm_type(self) -> str:
        return "spike-scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChat":
        return self

    def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kw: Any):
        first = next(m for m in messages if isinstance(m, HumanMessage))
        key = str(first.content)
        steps = SCRIPTS[key]
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < len(steps):
            tool_name, args = steps[done]
            reply = _call(tool_name, args, f"call-{key}-{done}")
        else:
            reply = AIMessage(content=f"done: {key}")
        return ChatResult(generations=[ChatGeneration(message=reply)])


class Recorder(AgentMiddleware):
    """Logs every model call and tool call it sees: who (lc_agent_name) and under which thread."""

    def __init__(self, label: str, log: list[dict]) -> None:
        super().__init__()
        self.label = label
        self.log = log

    def _see(self, hook: str, tool_name: str | None) -> None:
        cfg = get_config()
        self.log.append(
            {
                "recorder": self.label,
                "hook": hook,
                "tool": tool_name,
                "agent": cfg["metadata"].get("lc_agent_name"),
                "thread_id": cfg["configurable"].get("thread_id"),
                "ns": cfg["configurable"].get("checkpoint_ns", ""),
            }
        )

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        self._see("tool", request.tool_call["name"])
        return handler(request)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        self._see("model", None)
        return handler(request)


def run(agent: Any, key: str) -> dict:
    result = agent.invoke(
        {"messages": [HumanMessage(content=key)]},
        config={"configurable": {"thread_id": THREAD}, "recursion_limit": 50},
    )
    return result


def seen(log: list[dict], recorder: str, hook: str) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for row in log:
        if row["recorder"] == recorder and row["hook"] == hook:
            out[str(row["agent"])] += 1
    return dict(out)


def scenario_a() -> tuple[dict, list[dict]]:
    """Main + default general-purpose + two declared subagents (one with its own middleware)."""
    log: list[dict] = []
    model = ScriptedChat()
    agent = create_deep_agent(
        model=model,
        tools=[write_note],
        name="main",
        middleware=[Recorder("main-list", log)],
        subagents=[
            SubAgent(
                name="worker",
                description="declared subagent with its own middleware",
                system_prompt="worker",
                model=model,
                middleware=[Recorder("worker-spec", log)],
            ),
            SubAgent(
                name="plain",
                description="declared subagent with no middleware key",
                system_prompt="plain",
                model=model,
            ),
        ],
        checkpointer=InMemorySaver(),
    )
    return run(agent, "start-A"), log


def scenario_b() -> tuple[dict, list[dict]]:
    """Three levels: main -> mid (a deep agent) -> leaf. Each level carries its own recorder."""
    log: list[dict] = []
    model = ScriptedChat()
    inner = create_deep_agent(
        model=model,
        name="mid",
        middleware=[Recorder("mid", log)],
        subagents=[
            SubAgent(
                name="leaf",
                description="the third level",
                system_prompt="leaf",
                model=model,
                middleware=[Recorder("leaf", log)],
            )
        ],
    )
    agent = create_deep_agent(
        model=model,
        name="main",
        middleware=[Recorder("main", log)],
        subagents=[
            CompiledSubAgent(name="mid", description="second level, a deep agent", runnable=inner)
        ],
        checkpointer=InMemorySaver(),
    )
    return run(agent, "start-B"), log


def scenario_c() -> tuple[dict, list[dict]]:
    """A declared subagent named general-purpose replaces the default: does its middleware run?"""
    log: list[dict] = []
    model = ScriptedChat()
    agent = create_deep_agent(
        model=model,
        name="main",
        subagents=[
            SubAgent(
                name="general-purpose",
                description="override of the default",
                system_prompt="gp",
                model=model,
                middleware=[Recorder("gp-spec", log)],
            )
        ],
        checkpointer=InMemorySaver(),
    )
    return run(agent, "start-C"), log


def main() -> int:
    # Q2: the app above uses deepagents, langchain and langgraph only.
    out_a, log_a = scenario_a()
    files = sorted(out_a.get("files", {}))
    note(f"A files: {files}")
    for row in log_a:
        note(f"A {row}")
    wrote_all = {"/main.txt", "/gp.txt", "/worker.txt", "/plain.txt"} <= set(files)
    no_refapp = not any(m == "refapp" or m.startswith("refapp.") for m in sys.modules)
    no_key = not any(k.endswith("_API_KEY") and os.environ.get(k) for k in os.environ)
    q2_ok = wrote_all and no_refapp and no_key and not CONNECTS
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    print(
        f"{'PASS' if q2_ok else 'FAIL'}: Q2 outside app (create_deep_agent, scripted chat model, "
        f"own tool, 3 subagents, python {py}) ran offline: {len(log_a)} hook calls, files "
        f"{len(files)}/4 written, {len(CONNECTS)} socket connects, "
        f"refapp imported: {not no_refapp}, API keys in env: {not no_key}."
    )

    # Q3: which recorder sees which agent.
    main_tools = seen(log_a, "main-list", "tool")
    main_models = seen(log_a, "main-list", "model")
    spec_tools = seen(log_a, "worker-spec", "tool")
    spec_models = seen(log_a, "worker-spec", "model")
    note(f"main-list tool={main_tools} model={main_models}")
    note(f"worker-spec tool={spec_tools} model={spec_models}")
    _, log_c = scenario_c()
    gp_spec = seen(log_c, "gp-spec", "tool")
    note(f"gp-spec (override) tool={gp_spec}")
    main_sees_main = "main" in main_tools and "main" in main_models
    main_sees_gp = "general-purpose" in main_tools or "general-purpose" in main_models
    main_sees_worker = "worker" in main_tools or "worker" in main_models
    main_sees_plain = "plain" in main_tools or "plain" in main_models
    spec_sees_worker = "worker" in spec_tools and "worker" in spec_models
    spec_elsewhere = set(spec_tools) | set(spec_models)
    q3_ok = main_sees_main and spec_sees_worker and spec_elsewhere == {"worker"}
    print(
        f"{'PASS' if q3_ok else 'FAIL'}: Q3 middleware=[...] given to create_deep_agent sees: main "
        f"agent {main_sees_main}, default general-purpose subagent {main_sees_gp}, declared "
        f"subagent with no middleware key {main_sees_plain}, declared subagent with its own "
        f"middleware key {main_sees_worker}; a SubAgent spec's middleware sees that subagent "
        f"only: {spec_sees_worker and spec_elsewhere == {'worker'}}; a spec named general-purpose "
        f"(replaces the default) with middleware sees its own calls: "
        f"{'general-purpose' in gp_spec}."
    )

    # Q4: thread ids across three levels.
    out_b, log_b = scenario_b()
    for row in log_b:
        note(f"B {row}")
    levels = {}
    for row in log_b:
        levels.setdefault((row["recorder"], row["agent"]), set()).add(row["thread_id"])
    threads = {t for ts in levels.values() for t in ts}
    leaf_written = "/leaf.txt" in out_b.get("files", {}) or any(
        r["tool"] == "write_file" and r["agent"] == "leaf" for r in log_b
    )
    note(f"B levels {levels}")
    ns_depths = {
        r["agent"]: r["ns"].count("|") + (1 if r["ns"] else 0) for r in log_b if r["hook"] == "tool"
    }
    note(f"B checkpoint_ns depth by agent {ns_depths}")
    shown = ", ".join(
        f"{agent}={sorted(map(str, ts))[0]}" for (_, agent), ts in sorted(levels.items(), key=str)
    )
    q4_ok = leaf_written and threads == {THREAD}
    print(
        f"{'PASS' if q4_ok else 'FAIL'}: Q4 thread_id seen by middleware at 3 levels ({shown}); "
        f"subagents share the parent's thread_id: {threads == {THREAD}} "
        f"(distinct ids seen: {len(threads)}; leaf reached: {leaf_written})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
