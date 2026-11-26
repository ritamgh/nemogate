"""Per-thread guard state: hold-back counters and approvals, never shared between threads."""

import json
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_THREAD = "default"


class GuardState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocks: dict[tuple[str, str, str, str], int] = {}
        self._approvals: set[tuple[str, str]] = set()
        self._warned = False

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
        """Count one more block of this identical call and return the new count."""
        key = (thread, rule_id, tool, self.canonical_args(args))
        with self._lock:
            self._blocks[key] = self._blocks.get(key, 0) + 1
            return self._blocks[key]

    def record_approval(self, thread: str, rule_id: str) -> None:
        with self._lock:
            self._approvals.add((thread, rule_id))

    def has_approval(self, thread: str, rule_id: str) -> bool:
        with self._lock:
            return (thread, rule_id) in self._approvals
