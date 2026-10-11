"""Run ids, run config, scoped ledger, reproducibility fields and run folder files.

The contract is in replay/README.md, section "record.py".
"""

import hashlib
import json
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel

from replay.app import AppTask
from schemas.config import AppConfig
from schemas.enums import Arm, Operator, Role
from schemas.ledger import Ledger
from schemas.run import RunConfig

ROOT = Path(__file__).resolve().parents[1]  # the checkout this code runs from


def new_run_id() -> str:
    """`r-<UTC %Y%m%dT%H%M%S>-<6 lowercase hex>`."""
    return f"r-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo_dir, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise ValueError(f"cannot run git in {repo_dir}: {exc}") from exc


def git_sha(ref: str, repo_dir: Path = ROOT) -> str:
    """Full 40-hex commit sha of `ref` in `repo_dir`; ValueError if it is not a commit."""
    if not ref or ref.startswith("-"):
        raise ValueError(f"not a valid git ref: {ref!r}")
    result = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        detail = result.stderr.strip() or "unknown ref"
        raise ValueError(f"cannot resolve git ref {ref!r} in {repo_dir}: {detail}")
    return sha


def require_checkout_at(ref: str, repo_dir: Path = ROOT, *, allow_dirty: bool = False) -> str:
    """The sha of `ref`, provided it is what is checked out (the code that runs is this checkout).

    ValueError if `ref` is not HEAD, or if tracked files differ from HEAD or there are untracked
    files that .gitignore does not cover, and not `allow_dirty`. Ignored files never count.
    """
    sha = git_sha(ref, repo_dir)
    head = git_sha("HEAD", repo_dir)
    if sha != head:
        raise ValueError(
            f"ref {ref!r} is {sha[:12]} but the checkout is at {head[:12]}; "
            "a run can only claim the commit it runs from"
        )
    if not allow_dirty:
        # Explicit, so a user's status.showUntrackedFiles=no cannot hide new files.
        status = _git(repo_dir, "status", "--porcelain", "--untracked-files=normal")
        if status.returncode != 0:
            raise ValueError(f"git status failed in {repo_dir}: {status.stderr.strip()}")
        if status.stdout.strip():
            raise ValueError(
                f"the working tree differs from HEAD in {repo_dir}:\n{status.stdout.rstrip()}"
            )
    return sha


def lockfile_hash(path: Path = ROOT / "uv.lock") -> str:
    """First 12 hex of the sha256 of the lockfile's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def model_ids(config: AppConfig) -> dict[Role, str]:
    """Every role's configured model id; ValueError if a role has none."""
    ids: dict[Role, str] = {}
    for role in Role:
        model_id = config.models[role].id
        if not model_id:
            raise ValueError(f"config has no model id for role {role.value}")
        ids[role] = model_id
    return ids


def scoped_ledger(ledger: Ledger, constraint_id: str) -> Ledger:
    """A ledger holding only `constraint_id`; KeyError if it is not in `ledger`."""
    return Ledger(version=ledger.version, constraints=(ledger.get(constraint_id),))


def ledger_hash(ledger: Ledger) -> str:
    """First 12 hex of the sha256 of the ledger's compact, sorted-key JSON."""
    canonical = json.dumps(ledger.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def write_ledger(ledger: Ledger, path: Path) -> None:
    """Write `ledger` as YAML that `schemas.ledger.load_ledger_yaml` reads back to an equal one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(ledger.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8")


def make_run_config(
    *,
    run_id: str,
    task: AppTask,
    arm: Arm,
    operator: Operator,
    run_dir: Path,
    ledger_path: Path,
    seed: int,
    repo_ref: str,
) -> RunConfig:
    return RunConfig(
        run_id=run_id,
        task_id=task.id,
        arm=arm,
        operator=operator,
        target_boundary=task.boundary,
        target_constraint=task.constraint,
        repo_ref=repo_ref,
        ledger_path=str(ledger_path),
        trace_path=str(run_dir / "trace.jsonl"),
        seed=seed,
    )


def write_json(path: Path, model: BaseModel | list[BaseModel]) -> None:
    """Pretty JSON (indent 2, trailing newline) of `model_dump(mode="json")`; parents created."""
    if isinstance(model, list):
        data = [item.model_dump(mode="json") for item in model]
    else:
        data = model.model_dump(mode="json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
