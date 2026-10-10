"""The spend log and the spend cap (AGENTS.md rule 3).

Reserve, then settle. Before a live call is sent, `admit` appends a `reserve` line priced at an
estimate, but only if spent + estimate stays within the cap; the check and the append happen
under one exclusive `fcntl.flock`, so parallel processes cannot both claim the last room. When
the call completes, a `settle` line with the real usage replaces the reserve in the total. A
call that fails, is interrupted or returns no usage never settles, so it stays counted at its
estimate: the total can over-count but never under-count. There is deliberately no way to raise
or bypass the cap.
"""

import fcntl
import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from common import env
from schemas.config import AppConfig, ModelConfig
from schemas.enums import Role

_run_id: ContextVar[str | None] = ContextVar("nemogate_run_id", default=None)


class SpendCapReached(RuntimeError):
    """Total spend has reached the cap. Stop and tell a human."""


@lru_cache
def _default_log_path(repo_root: Path) -> Path:
    """`runs/spend.jsonl` of the main checkout, so every git worktree shares one log."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.split("\n")
        # Only trust git when repo_root is itself the checkout, not a folder inside another repo.
        if len(out) >= 2 and Path(out[0]).resolve() == repo_root.resolve():
            return Path(out[1]).parent / "runs" / "spend.jsonl"
    except (OSError, subprocess.SubprocessError):
        pass
    return repo_root / "runs" / "spend.jsonl"


def spend_log_path() -> Path:
    override = os.environ.get("NEMOGATE_SPEND_LOG")
    return Path(override) if override else _default_log_path(env.REPO_ROOT)


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


def _record(kind: str, call_id: str, role: Role | str, model_id: str, **fields: Any) -> str:
    return json.dumps(
        {
            "kind": kind,
            "call_id": call_id,
            "ts": datetime.now(UTC).isoformat(),
            "role": Role(role).value,
            "model_id": model_id,
            **fields,
            "run_id": _run_id.get(),
        }
    )


def _parse(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _spent(lines: list[dict]) -> float:
    """Σ settle cost + Σ cost of reserves with no settle of the same call_id."""
    settled = {line["call_id"] for line in lines if line.get("kind") == "settle"}
    return sum(
        line["cost_usd"]
        for line in lines
        if line.get("kind") in (None, "settle")
        or (line["kind"] == "reserve" and line["call_id"] not in settled)
    )


def append_spend(
    role: Role | str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    log_path: Path | str | None = None,
    call_id: str | None = None,
) -> None:
    """Append a `settle` line: the real usage and cost of one call (replacing its reserve)."""
    path = Path(log_path) if log_path is not None else spend_log_path()
    line = _record(
        "settle",
        call_id or uuid.uuid4().hex,
        role,
        model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
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
            return _parse(f.read())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def total_spent(log_path: Path | str | None = None) -> float:
    return _spent(_read_lines(log_path))


def run_totals(run_id: str, log_path: Path | str | None = None) -> tuple[dict[Role, int], float]:
    """Tokens per role (input + output; every role present) and cost for one run.

    Tokens come from settle lines only; cost also includes the run's unsettled reserves.
    """
    lines = [line for line in _read_lines(log_path) if line.get("run_id") == run_id]
    tokens = {role: 0 for role in Role}
    for line in lines:
        if line.get("kind") in (None, "settle"):
            tokens[Role(line["role"])] += line["input_tokens"] + line["output_tokens"]
    return tokens, _spent(lines)


def _cap_error(
    config: AppConfig, cap: float, spent: float, estimate: float = 0.0
) -> SpendCapReached:
    projected = f" plus ${estimate:.4f} reserved for this call" if estimate else ""
    return SpendCapReached(
        f"Spend cap reached: ${spent:.4f} spent{projected}, cap is ${cap:.4f} "
        f"({config.budget.stop_at_fraction:.0%} of ${config.budget.total_usd}). "
        "The cap cannot be raised; stop and tell a human (AGENTS.md rule 3)."
    )


def admit(
    config: AppConfig,
    role: Role | str,
    model_id: str,
    call_id: str,
    est_input_tokens: int,
    est_output_tokens: int,
    model_cfg: ModelConfig,
) -> None:
    """Reserve the estimated cost of one call, or raise SpendCapReached if it would pass the cap.

    Reading the total, comparing and appending the reserve all happen under one exclusive lock.
    """
    cap = config.budget.total_usd * config.budget.stop_at_fraction
    estimate = cost_usd(est_input_tokens, est_output_tokens, model_cfg)
    line = _record(
        "reserve",
        call_id,
        role,
        model_id,
        est_input_tokens=est_input_tokens,
        est_output_tokens=est_output_tokens,
        cost_usd=estimate,
    )
    path = spend_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            spent = _spent(_parse(f.read()))
            if spent + estimate > cap:
                raise _cap_error(config, cap, spent, estimate)
            f.write(line + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def check_cap(config: AppConfig) -> None:
    """Read-only: raise SpendCapReached if the total has already reached the cap."""
    cap = config.budget.total_usd * config.budget.stop_at_fraction
    spent = total_spent()
    if spent >= cap:
        raise _cap_error(config, cap, spent)
