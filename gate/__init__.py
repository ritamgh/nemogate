"""Boundary gate (SPEC C2). make_middleware is the frozen cross-owner entry point.

The runner passes the returned list as BOTH build_team(middleware=...) and
build_team(subagent_middleware=...) (schemas/contracts.py). Every refusal happens here, before
any agent runs, so a run that would measure nothing never spends credit.
"""

from typing import Any

from langchain.agents.middleware import AgentMiddleware

from common.env import REPO_ROOT
from common.llm import get_model
from gate.middleware import GateMiddleware
from gate.operators import make_operator
from gate.trace import TraceWriter
from ledger import load_ledger, scope_to
from schemas.config import load_config
from schemas.enums import Arm, Operator, Role
from schemas.ledger import ApprovalBefore, Ledger, PathForbidden
from schemas.run import RunConfig


def _check_arm(run_config: RunConfig) -> None:
    if run_config.arm in (Arm.block_only, Arm.gated):
        raise NotImplementedError(f"the {run_config.arm.value} arm is built in week 2")
    if run_config.arm is Arm.natural and run_config.operator is not Operator.none:
        raise ValueError("the natural arm takes operator none")
    if run_config.arm is Arm.mutated and run_config.operator is Operator.none:
        raise ValueError("the mutated arm needs an operator other than none")


def _check_app(ledger: Ledger) -> None:
    check = ledger.constraints[0].check
    if isinstance(check, PathForbidden) and ledger.app.repo_root is None:
        raise ValueError(
            "path_forbidden needs app.repo_root in the ledger: tool paths are absolute"
        )
    if isinstance(check, ApprovalBefore) and ledger.app.approval_tool is None:
        raise ValueError("approval_before needs app.approval_tool in the ledger")


def build_middleware(run_config: RunConfig, *, helper: Any | None = None) -> list[AgentMiddleware]:
    """make_middleware with an injectable helper model (tests pass a stub)."""
    _check_arm(run_config)
    loaded = load_ledger(run_config.ledger_path)  # the FULL ledger: it carries the app block
    ledger = scope_to(loaded.ledger, run_config.target_constraint)
    _check_app(ledger)
    config = load_config(REPO_ROOT / "config.yaml")
    model_id, safe_hash = config.models[Role.sut].id, config.safe_config.hash
    if model_id is None or safe_hash is None:
        raise ValueError("config.yaml needs models.sut.id and safe_config.hash to stamp traces")
    writer = TraceWriter(
        run_config, model_id=model_id, safe_config_hash=safe_hash, ledger_hash=loaded.ledger_hash
    )
    mutate = None
    if run_config.operator is not Operator.none:
        model = helper if helper is not None else get_model(Role.helper)
        mutate = make_operator(run_config.operator, ledger.constraints[0].text, model)
    return [GateMiddleware(run_config, ledger, writer, mutate)]


def make_middleware(run_config: RunConfig) -> list[AgentMiddleware]:
    return build_middleware(run_config)


__all__ = ["build_middleware", "make_middleware"]
