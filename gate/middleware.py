"""GateMiddleware: one instance in the main agent's stack and in every subagent's (SPEC C2).

It tells the stacks apart by lc_agent_name (refapp/README.md, "Paths and agent names"), records
every boundary and tool call (file paths repo-relative, so the oracle needs no app facts), and
mutates the text crossing the run's target boundary only: a delegation's description, or a
subagent's reply on its way back. Text the helper wrote carries the helper's model id.

Approval events are written when the app's approval tool returns, with `approved` meaning the
tool did not error (the reference app always approves). A delete issued in the same model turn
as its approval request may be recorded before or after it, so it counts as not having waited.
"""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_config
from langgraph.types import Command

from gate.text import message_text as _text
from gate.trace import TraceWriter
from oracle.paths import PATH_ARG, repo_relative
from schemas.ledger import Ledger
from schemas.run import RunConfig
from schemas.trace import Approval, BoundaryIn, BoundaryOut, FinalOutput, ToolCall, ToolResult

TASK_TOOL = "task"  # Deep Agents' delegation tool (docs/FACTS.md section 4)
_NAME = re.compile(r"[A-Za-z0-9_-]+")


def main_agent_name(target_boundary: str) -> str:
    """The delegating agent: X in delegation:X->Y and in return:Y->X."""
    kind, _, rest = target_boundary.partition(":")
    if kind == "compaction":
        return rest
    sender, _, receiver = rest.partition("->")
    return sender if kind == "delegation" else receiver


def _message(result: Any) -> ToolMessage | None:
    if isinstance(result, ToolMessage):
        return result
    if isinstance(result, Command) and isinstance(result.update, dict):
        messages = result.update.get("messages") or []
        if messages and isinstance(messages[-1], ToolMessage):
            return messages[-1]
    return None


def _with_text(result: Any, text: str) -> Any:
    """`result` with its tool-message text replaced: what the receiving agent will see."""
    if isinstance(result, ToolMessage):
        return result.model_copy(update={"content": text})
    message = _message(result)
    if message is not None:
        messages = [*result.update["messages"][:-1], message.model_copy(update={"content": text})]
        update = {**result.update, "messages": messages}
        return Command(graph=result.graph, update=update, resume=result.resume, goto=result.goto)
    if isinstance(result, str):
        return text
    raise TypeError(f"cannot place mutated text in a {type(result).__name__} result")


@dataclass
class _Pending:
    kind: str  # "task" or "tool"
    agent: str
    call: dict = field(default_factory=dict)
    sub: str | None = None


class GateMiddleware(AgentMiddleware):
    def __init__(
        self,
        run_config: RunConfig,
        ledger: Ledger,
        writer: TraceWriter,
        mutate: Callable[[str], str] | None,
        *,
        mutator_model_id: str | None = None,
    ) -> None:
        super().__init__()
        self._cfg = run_config
        self._ledger = ledger
        self._writer = writer
        self._mutate = mutate
        self._mutator_model_id = mutator_model_id
        self._main = main_agent_name(run_config.target_boundary)

    def _mutated(self, boundary: str, text: str) -> tuple[str, str | None]:
        """The text the receiver gets at `boundary`, and the id of the model that wrote it
        (None when it passed through unchanged)."""
        if self._mutate is None or boundary != self._cfg.target_boundary:
            return text, None
        return self._mutate(text), self._mutator_model_id

    def _agent(self) -> str:
        return get_config()["metadata"].get("lc_agent_name") or self._main

    def _inbound(self, agent: str) -> str | None:
        return None if agent == self._main else f"delegation:{self._main}->{agent}"

    def _recorded_args(self, args: dict) -> dict:
        out = dict(args)
        if isinstance(out.get(PATH_ARG), str):
            out[PATH_ARG] = repo_relative(out[PATH_ARG], self._ledger.app.repo_root)
        return out

    def _before(self, request: Any) -> tuple[Any, _Pending]:
        call = request.tool_call
        agent = self._agent()
        sub = call["args"].get("subagent_type") if call["name"] == TASK_TOOL else None
        if agent == self._main and isinstance(sub, str) and _NAME.fullmatch(sub):
            boundary = f"delegation:{agent}->{sub}"
            sent = str(call["args"].get("description", ""))
            # Mutate first: a failure leaves no half-recorded handoff.
            received, writer_id = self._mutated(boundary, sent)
            if received != sent:
                args = {**call["args"], "description": received}
                request = request.override(tool_call={**call, "args": args})
            self._writer.emit(BoundaryOut, agent=agent, boundary=boundary, content=sent)
            self._writer.emit(
                BoundaryIn, agent=sub, boundary=boundary, content=received, model_id=writer_id
            )
            return request, _Pending("task", agent, call, sub)
        self._writer.emit(
            ToolCall,
            agent=agent,
            boundary=self._inbound(agent),
            tool=call["name"],
            args=self._recorded_args(call["args"]),
            blocked=False,
        )
        return request, _Pending("tool", agent, call)

    def _after(self, pending: _Pending, result: Any) -> Any:
        message = _message(result)
        text = _text(message.content) if message is not None else _text(result)
        if pending.kind == "task":
            boundary = f"return:{pending.sub}->{pending.agent}"
            received, writer_id = self._mutated(boundary, text)
            if received != text:
                result = _with_text(result, received)
            self._writer.emit(BoundaryOut, agent=pending.sub, boundary=boundary, content=text)
            self._writer.emit(
                BoundaryIn,
                agent=pending.agent,
                boundary=boundary,
                content=received,
                model_id=writer_id,
            )
            return result
        tool = pending.call["name"]
        is_error = message is not None and message.status == "error"
        boundary = self._inbound(pending.agent)
        self._writer.emit(
            ToolResult,
            agent=pending.agent,
            boundary=boundary,
            tool=tool,
            content=text,
            is_error=is_error,
        )
        if tool == self._ledger.app.approval_tool:
            args = pending.call["args"]
            reason = args.get("reason")
            self._writer.emit(
                Approval,
                agent=pending.agent,
                boundary=boundary,
                action=str(args.get("action", json.dumps(args, default=str))),
                reason=reason if isinstance(reason, str) else None,
                approved=not is_error,
            )
        return result

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        request, pending = self._before(request)
        return self._after(pending, handler(request))

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        # Operators call the helper synchronously, before the call (delegation) or after it
        # (return); a thread keeps the loop free, and asyncio.to_thread copies the context, so
        # get_config() still sees lc_agent_name.
        request, pending = await asyncio.to_thread(self._before, request)
        result = await handler(request)
        if pending.kind == "task" and self._mutate is not None:
            return await asyncio.to_thread(self._after, pending, result)
        return self._after(pending, result)

    def after_agent(self, state: Any, runtime: Any) -> None:
        if self._agent() != self._main:
            return None
        last = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None)
        if last is not None:
            self._writer.emit(FinalOutput, agent=self._main, content=_text(last.content))
        return None

    async def aafter_agent(self, state: Any, runtime: Any) -> None:
        return self.after_agent(state, runtime)
