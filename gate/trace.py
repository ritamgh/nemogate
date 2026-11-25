"""TraceWriter: every gate event, stamped, numbered and validated, one JSON line each."""

import threading
from pathlib import Path
from typing import Any

from schemas.enums import Arm, Operator
from schemas.run import RunConfig
from schemas.trace import dump_event_jsonl

# What the guard stamps where the pipeline stamps a model id or a config hash: the guard calls no
# model and reads no config.yaml, but the event schema needs a non-empty value.
NO_MODEL = "none"


class TraceWriter:
    def __init__(
        self, run_config: RunConfig, *, model_id: str, safe_config_hash: str, ledger_hash: str
    ) -> None:
        self._path = Path(run_config.trace_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=False)  # FileExistsError: never mix two runs' traces
        self._stamp = {
            "run_id": run_config.run_id,
            "arm": run_config.arm,
            "operator": run_config.operator,
            "model_id": model_id,
            "safe_config_hash": safe_config_hash,
            "ledger_hash": ledger_hash,
        }
        self._step = 0
        self._lock = threading.Lock()

    def preview(self, model: type, *, step: int | None = None, **fields: Any):
        """The event `emit` would write next, validated but not written (for pre-call checks).

        `step` replaces the next step number, for a check that needs events in a fixed order."""
        with self._lock:
            return model(**self._stamp, step=self._step if step is None else step, **fields)

    def emit(self, model: type, *, model_id: str | None = None, **fields: Any):
        """Validate and append one event; the step increases only when the write succeeds.

        `model_id` overrides the stamped one for an event another model produced (the helper
        writing a mutated handoff).
        """
        stamp = self._stamp if model_id is None else {**self._stamp, "model_id": model_id}
        with self._lock:
            event = model(**stamp, step=self._step, **fields)
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(dump_event_jsonl(event) + "\n")
            self._step += 1
            return event


class GuardTraceWriter(TraceWriter):
    """The guard's trace: no run config, no model; a file only when `path` is given.

    An existing file is appended to, so an app restarted with the same trace_path keeps its
    history (step numbers start again at 0 for each Guard)."""

    def __init__(self, path: str | Path | None, *, ledger_hash: str) -> None:
        self._path = Path(path) if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch()
        self._stamp = {
            "run_id": "guard",
            "arm": Arm.gated,
            "operator": Operator.none,
            "model_id": NO_MODEL,
            "safe_config_hash": NO_MODEL,
            "ledger_hash": ledger_hash,
        }
        self._step = 0
        self._lock = threading.Lock()
