"""The spend log and the spend cap (AGENTS.md rule 3).

One JSONL line per completed live model call. Appends and reads hold an `fcntl.flock`, so
parallel processes neither interleave nor lose lines. There is deliberately no way to raise or
bypass the cap.
"""

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from common.env import REPO_ROOT
from schemas.config import AppConfig, ModelConfig
from schemas.enums import Role

_run_id: ContextVar[str | None] = ContextVar("nemogate_run_id", default=None)


class SpendCapReached(RuntimeError):
    """Total spend has reached the cap. Stop and tell a human."""


def spend_log_path() -> Path:
    override = os.environ.get("NEMOGATE_SPEND_LOG")
    return Path(override) if override else REPO_ROOT / "runs" / "spend.jsonl"


def cost_usd(input_tokens: int, output_tokens: int, model_cfg: ModelConfig) -> float:
    assert model_cfg.input_usd_per_mtok is not None
    assert model_cfg.output_usd_per_mtok is not None
    return (
        input_tokens * model_cfg.input_usd_per_mtok / 1e6
        + output_tokens * model_cfg.output_usd_per_mtok / 1e6
    )


@contextmanager
def run_scope(run_id: str) -> Iterator[None]:
    """Tag every spend line written inside the block with `run_id`."""
    token = _run_id.set(run_id)
    try:
        yield
    finally:
        _run_id.reset(token)


def append_spend(
    role: Role | str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    log_path: Path | str | None = None,
) -> None:
    path = Path(log_path) if log_path is not None else spend_log_path()
    line = json.dumps(
        {
            "ts": datetime.now(UTC).isoformat(),
            "role": Role(role).value,
            "model_id": model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
            "run_id": _run_id.get(),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read_lines(log_path: Path | str | None) -> list[dict]:
    path = Path(log_path) if log_path is not None else spend_log_path()
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return [json.loads(line) for line in f.read().splitlines() if line.strip()]
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def total_spent(log_path: Path | str | None = None) -> float:
    return sum(line["cost_usd"] for line in _read_lines(log_path))


def run_totals(run_id: str, log_path: Path | str | None = None) -> tuple[dict[Role, int], float]:
    """Tokens per role (input + output; every role present) and cost for one run."""
    tokens = {role: 0 for role in Role}
    cost = 0.0
    for line in _read_lines(log_path):
        if line["run_id"] == run_id:
            tokens[Role(line["role"])] += line["input_tokens"] + line["output_tokens"]
            cost += line["cost_usd"]
    return tokens, cost


def check_cap(config: AppConfig) -> None:
    cap = config.budget.total_usd * config.budget.stop_at_fraction
    spent = total_spent()
    if spent >= cap:
        raise SpendCapReached(
            f"Spend cap reached: ${spent:.4f} spent, cap is ${cap:.4f} "
            f"({config.budget.stop_at_fraction:.0%} of ${config.budget.total_usd}). "
            "The cap cannot be raised; stop and tell a human (AGENTS.md rule 3)."
        )
