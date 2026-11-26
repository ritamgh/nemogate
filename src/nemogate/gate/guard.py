"""The rule guard: a rules file (YAML) loaded into the ledger models the oracle already uses.

A rule names itself with a slug and carries text and one check; the loader fills every other
ledger field with a fixed default. The guard is driven by the rules file alone: no run config,
no repo config.yaml, no .env.

A rule's check is one of `path_forbidden: {glob, ops}`, `tool_forbidden: {name}`,
`approval_before: {tool}` or `tool_arg_bound: {tool, arg, op, value}`. The last states the
allowed condition: `{tool: refund, arg: amount, op: "<=", value: 100}` lets amount 100 through
and blocks 250. See oracle.arg_bound_problem for how a missing, null or mistyped argument fails.
"""

import collections.abc
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from langchain.agents.middleware import AgentMiddleware
from pydantic import ValidationError

from nemogate.gate.middleware import GuardMiddleware
from nemogate.gate.state import GuardState
from nemogate.gate.trace import GuardTraceWriter
from nemogate.ledger import ledger_hash
from nemogate.schemas.enums import FileOp
from nemogate.schemas.ledger import AppInfo, Constraint, Ledger

log = logging.getLogger(__name__)

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


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping key written twice: PyYAML lets the last one win, which
    would drop rules or change a rule without a word."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=True)
            if isinstance(key, collections.abc.Hashable):
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"found duplicate key {key!r}", key_node.start_mark
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _validation_message(exc: ValidationError) -> str:
    """The pydantic message without its trailing "For further information visit ..." lines."""
    lines = str(exc).splitlines()
    return "\n".join(line for line in lines if "errors.pydantic.dev" not in line)


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
        trace_path: str | Path | None = None,
    ) -> None:
        self.rules = rules
        self.approval_tool = approval_tool
        self.tool_map = tool_map
        self.trace_path = trace_path
        self.state = GuardState()
        self._writer: GuardTraceWriter | None = None

    @classmethod
    def from_yaml(cls, path: str | Path, *, trace_path: str | Path | None = None) -> "Guard":
        """Load a rules file; ValueError names the file (and the rule and field) at fault.

        The guard writes a JSONL trace of its decisions to `trace_path` when one is given, and
        no file otherwise."""
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{path}: not valid UTF-8 text") from exc
        try:
            data = yaml.load(text, Loader=_UniqueKeyLoader)
        except yaml.YAMLError as exc:
            raise ValueError(f"{path}: not valid YAML: {exc}") from exc
        if data is None:
            raise ValueError(
                f"{path}: the file is empty; expected a mapping with `version:` and `rules:`"
            )
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
            raise ValueError(f"{path}: {_validation_message(exc)}") from exc
        if not rules:
            log.warning("%s: no rules: the guard will allow every call", path)
        return cls(rules, approval_tool, tool_map, trace_path)

    def middleware(self) -> list[AgentMiddleware]:
        """The list for create_deep_agent(middleware=) and for each SubAgent spec's middleware.

        An approval tool (the file's approval_tool) approves when it returns without error and
        its answer is not a denial. Raise, or return {"approved": false}, to deny: a mapping, or
        JSON text of one, whose "approved" or "approve" is false, "false" or 0.

        Every call shares this guard's state and trace, so the main agent's and each subagent's
        instance count blocks and approvals together."""
        ledger = Ledger(
            version=1,
            app=AppInfo(approval_tool=self.approval_tool),
            constraints=tuple(r.constraint for r in self.rules),
        )
        if self._writer is None:
            self._writer = GuardTraceWriter(self.trace_path, ledger_hash=ledger_hash(ledger))
        return [GuardMiddleware(self.rules, ledger, self.state, self.tool_map, self._writer)]


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


def _normalise_glob(glob: str, where: str) -> str:
    """Make a glob comparable with the repo-relative paths the oracle matches it against.

    `./app/**` would otherwise never match `app/x` and silently protect nothing. Drops `.`
    segments and repeated slashes (a trailing slash stays: `app/` matches the folder itself)."""
    if glob.startswith("/"):
        raise ValueError(f"{where}: glob {glob!r} must be relative to the repo, not absolute")
    segments = glob.split("/")
    if ".." in segments:
        raise ValueError(f"{where}: glob {glob!r} must not contain a '..' segment")
    kept = [s for s in segments if s not in ("", ".")]
    if not kept:
        raise ValueError(f"{where}: glob {glob!r} matches no path")
    return "/".join(kept) + ("/" if glob.endswith("/") else "")


def _check_arg_bound(params: dict, where: str) -> None:
    """Refuse a bound that would block every call or mean something else than written."""
    for field in ("tool", "arg"):
        if isinstance(params.get(field), str) and not params[field].strip():
            raise ValueError(f"{where}: tool_arg_bound field {field!r} must not be empty")
    arg = params.get("arg")
    if isinstance(arg, str) and (arg != arg.strip() or "." in arg):
        raise ValueError(
            f"{where}: tool_arg_bound field 'arg' must be the exact name of a top-level argument "
            f"(no surrounding spaces, no '.'), got {arg!r}"
        )
    value = params.get("value")
    if isinstance(value, bool):  # the model would read true as 1
        raise ValueError(
            f"{where}: tool_arg_bound field 'value' must be a number or text, got a bool"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{where}: tool_arg_bound field 'value' must be finite, got {value!r}")
    op = params.get("op")
    if op in ("<", "<=", ">=", ">") and isinstance(value, str):
        raise ValueError(
            f"{where}: tool_arg_bound field 'value' must be a number for op {op!r}, "
            f"got the text {value!r}"
        )


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
    if kind == "path_forbidden" and isinstance(params.get("glob"), str):
        params = {**params, "glob": _normalise_glob(params["glob"], where)}
    if kind == "path_forbidden" and "ops" in params and not isinstance(params["ops"], list):
        raise ValueError(
            f"{where}, check 'path_forbidden': ops must be a list such as "
            f"[{', '.join(op.value for op in FileOp)}], got {params['ops']!r}"
        )
    if kind == "tool_arg_bound":
        _check_arg_bound(params, where)
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
        raise ValueError(f"{where}: {_validation_message(exc)}") from exc
    return Rule(constraint, mode)
