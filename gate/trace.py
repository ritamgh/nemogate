"""TraceWriter: every gate event, stamped, numbered and validated, one JSON line each."""

import threading
from pathlib import Path
from typing import Any

from schemas.run import RunConfig
from schemas.trace import dump_event_jsonl


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

    def emit(self, model: type, **fields: Any):
        """Validate and append one event; the step increases only when the write succeeds."""
        with self._lock:
            event = model(**self._stamp, step=self._step, **fields)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(dump_event_jsonl(event) + "\n")
            self._step += 1
            return event
