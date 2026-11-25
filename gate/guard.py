"""The rule guard: a rules file (YAML) loaded into the ledger models the oracle already uses.

A rule names itself with a slug and carries text and one check; the loader fills every other
ledger field with a fixed default. The guard is driven by the rules file alone: no run config,
no repo config.yaml, no .env.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from gate.state import GuardState
from schemas.enums import FileOp
from schemas.ledger import AppInfo, Constraint, Ledger

_TOP_FIELDS = {"version", "approval_tool", "tools", "rules"}
_RULE_FIELDS = {"id", "text", "check", "mode"}
_CHECK_FIELDS = {
    "path_forbidden": {"glob", "ops"},
    "tool_forbidden": {"name"},
    "approval_before": {"tool"},
    "tool_arg_bound": {"tool", "arg", "op", "value"},
}
_TOOL_FIELDS = {"path_arg", "op"}
_MODES = ("block", "observe")
# Filled by the loader: the file does not say. The guard enforces a rule for every agent, so the
# scope is a placeholder name, not a filter.
_DEFAULTS = {
    "kind": "constraint",
    "source": "user",
    "authority": "binding",
    "scope": ["all"],
    "origin": "declared",
}


@dataclass(frozen=True)
class Rule:
    constraint: Constraint
    mode: str  # "block" or "observe"

    @property
    def id(self) -> str:
        return self.constraint.id

    @property
    def text(self) -> str:
        return self.constraint.text


class Guard:
    def __init__(
        self,
        rules: list[Rule],
        approval_tool: str | None,
        tool_map: dict[str, dict[str, str]],
    ) -> None:
        self.rules = rules
        self.approval_tool = approval_tool
        self.tool_map = tool_map
        self.state = GuardState()

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Guard":
        """Load a rules file; ValueError names the file (and the rule and field) at fault."""
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: the rules file must be a mapping")
        _reject_unknown(data, _TOP_FIELDS, f"{path}")
        if data.get("version") != 1:
            raise ValueError(f"{path}: version must be 1, got {data.get('version')!r}")
        approval_tool = data.get("approval_tool")
        tool_map = _load_tools(path, data.get("tools") or {})
        raw_rules = data.get("rules")
        if not isinstance(raw_rules, list):
            raise ValueError(f"{path}: rules must be a list")
        rules = [_load_rule(path, raw, approval_tool) for raw in raw_rules]
        try:  # the same model the oracle reads; it also rejects duplicate ids
            Ledger(
                version=1,
                app=AppInfo(approval_tool=approval_tool),
                constraints=tuple(r.constraint for r in rules),
            )
        except ValidationError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        return cls(rules, approval_tool, tool_map)

    def middleware(self):
        raise NotImplementedError("wired in T-001b")


def _reject_unknown(mapping: dict, allowed: set[str], where: str) -> None:
    for field in mapping:
        if field not in allowed:
            raise ValueError(f"{where}: unknown field {field!r}")


def _load_tools(path: Path, tools: Any) -> dict[str, dict[str, str]]:
    if not isinstance(tools, dict):
        raise ValueError(f"{path}: tools must be a mapping of tool name to path_arg and op")
    mapped = {}
    for name, entry in tools.items():
        where = f"{path}: tool {name!r}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: must be a mapping with path_arg and op")
        _reject_unknown(entry, _TOOL_FIELDS, where)
        if not isinstance(entry.get("path_arg"), str) or not entry["path_arg"].strip():
            raise ValueError(f"{where}: path_arg is required")
        if entry.get("op") not in {op.value for op in FileOp}:
            raise ValueError(f"{where}: op must be one of {[op.value for op in FileOp]}")
        mapped[name] = {"path_arg": entry["path_arg"], "op": entry["op"]}
    return mapped


def _load_rule(path: Path, raw: Any, approval_tool: str | None) -> Rule:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: each rule must be a mapping, got {raw!r}")
    rule_id = raw.get("id")
    where = f"{path}: rule {rule_id!r}"
    _reject_unknown(raw, _RULE_FIELDS, where)
    for field in ("id", "text", "check"):
        if field not in raw:
            raise ValueError(f"{where}: missing field {field!r}")
    mode = raw.get("mode", "block")
    if mode not in _MODES:
        raise ValueError(f"{where}: mode must be one of {list(_MODES)}, got {mode!r}")
    check = raw["check"]
    if not isinstance(check, dict) or len(check) != 1:
        raise ValueError(f"{where}: needs exactly one check of {sorted(_CHECK_FIELDS)}")
    ((kind, params),) = check.items()
    if kind not in _CHECK_FIELDS:
        raise ValueError(
            f"{where}: unknown check {kind!r}, expected one of {sorted(_CHECK_FIELDS)}"
        )
    if not isinstance(params, dict):
        raise ValueError(f"{where}: check {kind!r} must be a mapping")
    _reject_unknown(params, _CHECK_FIELDS[kind], f"{where}, check {kind!r}")
    if kind == "approval_before" and not approval_tool:
        raise ValueError(f"{where}: approval_before needs approval_tool at the top of the file")
    try:
        constraint = Constraint.model_validate(
            {
                "id": rule_id,
                "text": raw["text"],
                **_DEFAULTS,
                "check": {"type": kind, **params},
            }
        )
    except ValidationError as exc:
        raise ValueError(f"{where}: {exc}") from exc
    return Rule(constraint, mode)
