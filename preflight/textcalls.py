"""Tool calls written as text, found offline in run folders (the NAT #2274 failure class).

`python -m preflight.textcalls RUN_DIR_OR_PARENT... [--json OUT]` makes no model call and no sandbox
call. A model that writes its tool call into the message text instead of a native `tool_calls`
entry never has it executed, and the trace shows nothing: `trace.jsonl` records tool calls and
results, not ordinary AI messages. Those live in `checkpoints.sqlite` (docs/FACTS.md section 5), so
this reads the `messages` writes of every thread and namespace, the subagents' too, and applies the
detector the preflight probes use (`looks_like_tool_call_text`) to each AI message that has no
native tool call. It also checks the text that crosses a boundary in `trace.jsonl`
(`final_output`, `boundary_in`).

A fork's checkpoint file starts as a copy of its source's. Rows that also exist in the source's
file are the copied history and are skipped, so a fork reports only its own messages; if the
source folder cannot be found, the whole file is scanned and the run is marked
`prefix_separated: false`. A fork's `trace.jsonl` holds only its own events (the copied prefix is
in `prefix.jsonl`, which is not scanned). Run folders are never written. A hit is a candidate to
read, not a verdict.
"""

import argparse
import json
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from langchain_core.messages import AIMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from preflight.probes import _text, looks_like_tool_call_text

CHECKPOINTS = "checkpoints.sqlite"
TRACE = "trace.jsonl"
FORK = "fork.json"
RUN_FILES = ("run_config.json", "record.json", TRACE, CHECKPOINTS)  # what marks a run folder
TRACE_TYPES = ("final_output", "boundary_in")  # events whose content crossed a boundary as text
EXCERPT_CHARS = 200

_FUNCTION_TAG = re.compile(r"<function=([^>\s]+)")
_WRITE_KEY = "thread_id, checkpoint_ns, checkpoint_id, task_id, idx"


@dataclass(frozen=True)
class Candidate:
    run_id: str
    source: str  # "checkpoint" (an AI message) or "trace" (a boundary or final-output event)
    agent: str
    where: str  # checkpoint namespace for a message, event type for a trace line
    step: int | None
    index: int  # position among the namespace's AI messages, or the line number in trace.jsonl
    tool: str  # the tool the text looks like it calls; "?" when only a call marker was found
    excerpt: str


@dataclass
class RunScan:
    run_id: str
    ai_messages: int = 0
    trace_events: int = 0
    fork_of: str | None = None
    prefix_separated: bool | None = None  # None: not a fork
    agents: dict[str, int] = field(default_factory=dict)  # AI messages per agent
    candidates: list[Candidate] = field(default_factory=list)


@contextmanager
def _open_copy(path: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection to a copy of `path`, with its `-wal` file if it has one.

    Opening a WAL-mode file in place, even with `mode=ro`, creates `-shm` and `-wal` files beside
    it. The copy keeps the run folder byte-for-byte as it was and still sees uncheckpointed rows.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / path.name
        for suffix in ("", "-wal"):
            if Path(f"{path}{suffix}").is_file():
                shutil.copy(f"{path}{suffix}", f"{target}{suffix}")
        with closing(sqlite3.connect(f"file:{quote(str(target))}?mode=ro", uri=True)) as con:
            yield con


def _find_call(content: str) -> tuple[str, int]:
    """(tool name, offset) of the first call-shaped text in `content`; ("?", 0) if unnamed."""
    tag = _FUNCTION_TAG.search(content)
    if tag:
        return tag.group(1), tag.start()
    decoder = json.JSONDecoder()
    for start, char in enumerate(content):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(content, start)
            except ValueError:
                continue
            name = _call_name(value)
            if name:
                return name, start
    marker = min(
        (content.find(m) for m in ("<tool_call>", "[TOOL_CALLS]") if m in content), default=0
    )
    return "?", marker


def _call_name(value: Any) -> str | None:
    if isinstance(value, list):
        return next(filter(None, map(_call_name, value)), None)
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("name"), str) and any(
        k in value for k in ("arguments", "parameters", "args")
    ):
        return value["name"]
    return next(filter(None, map(_call_name, value.values())), None)


def _excerpt(content: str, offset: int) -> str:
    flat = " ".join(content[max(0, offset - 40) :].split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[: EXCERPT_CHARS - 3] + "..."


def _candidate(run_id, source, agent, where, step, index, content) -> Candidate | None:
    if not looks_like_tool_call_text(content):
        return None
    tool, offset = _find_call(content)
    return Candidate(run_id, source, agent, where, step, index, tool, _excerpt(content, offset))


def _step(metadata: bytes | None) -> int | None:
    return json.loads(metadata).get("step") if metadata else None


def _message_writes(path: Path, skip: set[tuple]) -> Iterator[tuple[str, int | None, Any]]:
    """(namespace, step, message) for each `messages` write, oldest first, minus `skip` rows."""
    serde = JsonPlusSerializer()
    query = """
        SELECT w.thread_id, w.checkpoint_ns, w.checkpoint_id, w.task_id, w.idx, w.type, w.value,
               c.metadata
        FROM writes w LEFT JOIN checkpoints c USING (thread_id, checkpoint_ns, checkpoint_id)
        WHERE w.channel = 'messages'
        ORDER BY w.thread_id, w.checkpoint_ns, w.checkpoint_id, w.idx
    """
    with _open_copy(path) as con:
        for *key, type_, value, metadata in con.execute(query):
            if tuple(key) in skip:
                continue
            payload = serde.loads_typed((type_, value))
            for message in payload if isinstance(payload, list) else [payload]:
                yield key[1], _step(metadata), message


def _write_keys(path: Path) -> set[tuple]:
    with _open_copy(path) as con:
        return set(con.execute(f"SELECT {_WRITE_KEY} FROM writes WHERE channel = 'messages'"))


def _trace_events(run_dir: Path) -> Iterator[tuple[int, dict]]:
    path = run_dir / TRACE
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle):
            if line.strip():
                yield number, json.loads(line)


def scan_run(run_dir: Path, known: dict[str, Path] | None = None) -> RunScan:
    """Scan one run folder. `known` maps run ids to folders, to find a fork's source."""
    scan = RunScan(run_id=run_dir.name)
    skip: set[tuple] = set()
    if (run_dir / FORK).is_file():
        scan.fork_of = json.loads((run_dir / FORK).read_text(encoding="utf-8"))["source_run_id"]
        source = (known or {}).get(scan.fork_of) or run_dir.parent / scan.fork_of
        scan.prefix_separated = (source / CHECKPOINTS).is_file()
        if scan.prefix_separated:
            skip = _write_keys(source / CHECKPOINTS)

    events = list(_trace_events(run_dir))
    root_agent = next((e["agent"] for _, e in events if e.get("boundary") is None), None)

    if (run_dir / CHECKPOINTS).is_file():
        seen: Counter = Counter()  # AI messages so far per namespace, for the index
        ids: set[tuple] = set()
        for namespace, step, message in _message_writes(run_dir / CHECKPOINTS, skip):
            if not isinstance(message, AIMessage) or (namespace, message.id) in ids:
                continue
            ids.add((namespace, message.id))
            agent = message.name or (root_agent if namespace == "" else None) or "?"
            scan.ai_messages += 1
            scan.agents[agent] = scan.agents.get(agent, 0) + 1
            index = seen[namespace]
            seen[namespace] += 1
            if message.tool_calls:
                continue
            hit = _candidate(
                scan.run_id,
                "checkpoint",
                agent,
                namespace or "root",
                step,
                index,
                _text(message.content),
            )
            if hit:
                scan.candidates.append(hit)

    for number, event in events:
        if event["type"] not in TRACE_TYPES:
            continue
        scan.trace_events += 1
        hit = _candidate(
            scan.run_id,
            "trace",
            event["agent"],
            event["type"],
            event["step"],
            number,
            str(event["content"]),
        )
        if hit:
            scan.candidates.append(hit)
    return scan


def find_runs(roots: Iterable[Path]) -> list[Path]:
    """Run folders at or under `roots` (holding a checkpoint file or a trace), never sealed ones."""
    found = set()
    for root in roots:
        for name in (CHECKPOINTS, TRACE):
            files = [root / name] if (root / name).is_file() else root.rglob(name)
            found.update(f.parent for f in files)
    return sorted(p for p in found if "heldout" not in str(p).lower())


def scan_all(roots: Iterable[Path]) -> list[RunScan]:
    runs = find_runs(roots)
    known = {run.name: run for run in runs}
    return [scan_run(run, known) for run in runs]


def _format(candidate: Candidate) -> str:
    step = "-" if candidate.step is None else candidate.step
    return (
        f"{candidate.run_id}  {candidate.source}  agent={candidate.agent}  {candidate.where}  "
        f"step={step}  #{candidate.index}  looks like: {candidate.tool}\n    {candidate.excerpt}"
    )


def report(scans: Sequence[RunScan]) -> str:
    candidates = [c for s in scans for c in s.candidates if c.source == "checkpoint"]
    from_trace = [c for s in scans for c in s.candidates if c.source == "trace"]
    agents: Counter = Counter()
    for s in scans:
        agents.update(s.agents)
    lines = [_format(c) for c in candidates + from_trace]
    lines.append("")
    lines.append(
        f"runs scanned {len(scans)}, AI messages {sum(s.ai_messages for s in scans)}, "
        f"candidates {len(candidates)} (runs with any: {len({c.run_id for c in candidates})})"
    )
    lines.append(
        f"boundary text in trace.jsonl: events {sum(s.trace_events for s in scans)}, "
        f"candidates {len(from_trace)} (runs with any: {len({c.run_id for c in from_trace})})"
    )
    lines.append(
        "AI messages by agent: " + ", ".join(f"{a} {n}" for a, n in sorted(agents.items()))
    )
    unseparated = [s.run_id for s in scans if s.prefix_separated is False]
    if unseparated:
        lines.append(f"forks whose source was not found, copied prefix counted: {len(unseparated)}")
    return "\n".join(lines)


def _json_target_problem(out: Path, roots: Iterable[Path]) -> str | None:
    """Why `out` may not be written, or None. Never replace a file, never write among the runs."""
    out = out.resolve()
    if out.exists():
        return "already exists"
    for root in roots:
        if out.is_relative_to(root.resolve()):
            return f"is inside the scanned folder {root}"
    for folder in out.parents:
        if any((folder / name).exists() for name in RUN_FILES):
            return f"is inside the run folder {folder}"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m preflight.textcalls", description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path, metavar="RUN_DIR_OR_PARENT")
    parser.add_argument("--json", type=Path, metavar="OUT", help="also write every result as JSON")
    args = parser.parse_args(argv)

    scans = scan_all(args.run_dirs)
    if not scans:
        parser.error("no run folder (checkpoints.sqlite or trace.jsonl) under the given paths")
    if args.json is not None:
        problem = _json_target_problem(args.json, args.run_dirs)
        if problem:
            parser.error(f"--json {args.json}: {problem}")
    print(report(scans))
    if args.json is not None:
        args.json.write_text(
            json.dumps([asdict(s) for s in scans], indent=2, default=dict) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
