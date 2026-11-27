"""Per-thread guard state: hold-back counters and approvals, never shared between threads."""

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_THREAD = "default"

# Memory bounds, so a long-running process cannot grow without limit. Past MAX_THREADS threads the
# least recently used thread loses its counters and approvals; past MAX_BLOCKS_PER_THREAD distinct
# blocked calls in one thread the least recently blocked call loses its counter.
MAX_THREADS = 10_000
MAX_BLOCKS_PER_THREAD = 256


@dataclass
class _Thread:
    blocks: OrderedDict[str, int] = field(default_factory=OrderedDict)  # call hash -> count
    approvals: set[str] = field(default_factory=set)  # rule ids


class GuardState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: OrderedDict[str, _Thread] = OrderedDict()  # least recently used first
        self._warned = False

    def _thread(self, thread: str) -> _Thread:
        """The state of `thread`, made if new and marked most recently used. Hold the lock."""
        entry = self._threads.get(thread)
        if entry is None:
            entry = self._threads[thread] = _Thread()
            while len(self._threads) > MAX_THREADS:
                self._threads.popitem(last=False)
        else:
            self._threads.move_to_end(thread)
        return entry

    def thread_key(self, config: dict[str, Any] | None) -> str:
        """The LangGraph thread id of `config`; runs without one share the default thread."""
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
        if thread_id is not None:
            return str(thread_id)
        with self._lock:
            warn = not self._warned
            self._warned = True
        if warn:
            log.warning(
                "no thread_id in the run config: counters and approvals of every run without "
                "one are shared under %r; pass config={'configurable': {'thread_id': ...}}",
                DEFAULT_THREAD,
            )
        return DEFAULT_THREAD

    def canonical_args(self, args: dict[str, Any]) -> str:
        return json.dumps(args, sort_keys=True, default=str)

    def record_block(self, thread: str, rule_id: str, tool: str, args: dict[str, Any]) -> int:
        """Count one more block of this identical call and return the new count. Only a short hash
        of (rule, tool, arguments) is stored, never the arguments."""
        digest = hashlib.sha256(
            f"{rule_id}\0{tool}\0{self.canonical_args(args)}".encode()
        ).hexdigest()[:32]
        with self._lock:
            blocks = self._thread(thread).blocks
            blocks[digest] = blocks.pop(digest, 0) + 1  # re-inserted: now the most recent
            while len(blocks) > MAX_BLOCKS_PER_THREAD:
                blocks.popitem(last=False)
            return blocks[digest]

    def record_approval(self, thread: str, rule_id: str) -> None:
        with self._lock:
            self._thread(thread).approvals.add(rule_id)

    def has_approval(self, thread: str, rule_id: str) -> bool:
        with self._lock:
            entry = self._threads.get(thread)
            if entry is None:
                return False
            self._threads.move_to_end(thread)
            return rule_id in entry.approvals
