"""Boundary recording and fork points: where a run can be forked from, and what to restore.

The contract is in replay/README.md, section "boundaries.py".
"""

import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from pydantic import BaseModel

from schemas.enums import STRICT, BoundaryId, NonEmptyStr

TASK_TOOL = "task"  # Deep Agents' delegation tool (docs/FACTS.md section 4)
_AGENT = re.compile(r"[A-Za-z0-9_-]+")  # an agent name a boundary id can hold


class Crossing(BaseModel):
    """One delegation (`task` call) and its return, with the checkpoint and image at each."""

    model_config = STRICT
    tool_call_id: NonEmptyStr
    delegation: BoundaryId  # delegation:<sender>-><subagent>
    returned: BoundaryId  # return:<subagent>-><sender>
    pre_checkpoint: NonEmptyStr  # root checkpoint of the step that runs the call: the fork point
    pre_image: NonEmptyStr  # session image when the delegation crossed
    post_checkpoint: NonEmptyStr | None  # root checkpoint right after that step; None if absent
    post_image: NonEmptyStr | None  # session image when the reply crossed; None if it never did


class Recording(BaseModel):
    """`boundaries.json`: every crossing of a run, in delegation order."""

    model_config = STRICT
    base_image: NonEmptyStr  # the image the app's run started from (a fork keeps its source's)
    thread_id: NonEmptyStr  # the checkpoint thread (a fork keeps its source's)
    crossings: tuple[Crossing, ...]


class ForkOrigin(BaseModel):
    """`fork.json`: the recorded run a fork continues, and the crossing it resumed before."""

    model_config = STRICT
    source_run_id: NonEmptyStr
    crossing: Crossing


def _sender(target_boundary: str) -> str:
    """The delegating agent: X in delegation:X->Y, in return:Y->X and in compaction:X."""
    kind, _, rest = target_boundary.partition(":")
    if kind == "compaction":
        return rest
    sender, _, receiver = rest.partition("->")
    return sender if kind == "delegation" else receiver


class BoundaryRecorder(AgentMiddleware):
    """Records each `task` call's checkpoint and the session's image as it crosses.

    The checkpoint is `checkpoint_map[""]` of the tool call's config, the root checkpoint the
    `tools` step runs from (`checkpoint_id` is None there; docs/FACTS.md section 5). The image is
    `session.uuid` read before the call runs and after it returns (section 3). The sender is the
    calling agent's `lc_agent_name`, else the sender of the run's target boundary, as the gate
    names it.
    """

    def __init__(self, session: Any, target_boundary: str) -> None:
        super().__init__()
        self._session = session
        self._sender = _sender(target_boundary)
        self._open: dict[str, dict[str, Any]] = {}  # tool call id -> fields; delegation order

    def _enter(self, request: Any) -> str | None:
        call = request.tool_call
        sub = call["args"].get("subagent_type")
        if call["name"] != TASK_TOOL or not isinstance(sub, str) or not _AGENT.fullmatch(sub):
            return None
        config = request.runtime.config
        agent = (config.get("metadata") or {}).get("lc_agent_name") or self._sender
        if not _AGENT.fullmatch(agent):
            return None  # no valid boundary id names this call: never let recording crash a run
        self._open[call["id"]] = {
            "tool_call_id": call["id"],
            "delegation": f"delegation:{agent}->{sub}",
            "returned": f"return:{sub}->{agent}",
            "pre_checkpoint": config["configurable"]["checkpoint_map"][""],
            "pre_image": str(self._session.uuid),
            "post_image": None,
        }
        return call["id"]

    def _leave(self, call_id: str | None) -> None:
        if call_id is not None:
            self._open[call_id]["post_image"] = str(self._session.uuid)

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        call_id = self._enter(request)
        result = handler(request)
        self._leave(call_id)
        return result

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        call_id = self._enter(request)
        result = await handler(request)
        self._leave(call_id)
        return result

    def crossings(self, children: Mapping[str, str]) -> tuple[Crossing, ...]:
        """Every crossing so far. `children` maps a root checkpoint id to the id of this run's
        checkpoint right after it; a crossing whose reply never came has no post checkpoint."""
        out = []
        for fields in self._open.values():
            replied = fields["post_image"] is not None
            post = children.get(fields["pre_checkpoint"]) if replied else None
            out.append(Crossing(**fields, post_checkpoint=post))
        return tuple(out)


def fork_point(recording: Recording, boundary: str) -> Crossing | None:
    """The crossing to fork before: the first whose delegation or return is `boundary`.

    The first, so every crossing of the boundary lies after the fork. A return forks before its
    delegation too: no root checkpoint lies between the subagent finishing and its reply, so the
    call runs again. None if the run never crossed `boundary`.
    """
    for crossing in recording.crossings:
        if boundary in (crossing.delegation, crossing.returned):
            return crossing
    return None


def copy_checkpoints(src: Path, dst: Path) -> None:
    """Copy a SQLite checkpoint database, including rows still in its write-ahead log."""
    if not src.is_file():
        raise FileNotFoundError(f"no checkpoint database at {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(src)) as source, closing(sqlite3.connect(dst)) as target:
        source.backup(target)
