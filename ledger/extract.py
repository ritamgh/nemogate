"""Helper-model extraction of rules and compilation into the closed check list (SPEC C1).

The helper proposes; a human confirms or rejects each proposal; confirmed rules are appended to
the ledger, which is append-only. A rule the helper cannot express as exactly one closed-list
check, or whose check does not validate, is kept unverified (`check: null`) and never guessed.
LLMs propose here; they never grade (AGENTS.md rule 7).

    NEMOGATE_LIVE=1 uv run python -m ledger.extract --ledger nemogate.yaml \
        --text-file PROMPT.txt --agents a,b --tools x,y [--dry-run | --yes]
"""

import argparse
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, TypeAdapter, ValidationError

from common.env import is_live
from schemas.enums import Role
from schemas.ledger import (
    ApprovalBefore,
    Check,
    Constraint,
    FilesFrozen,
    Ledger,
    OutputMustInclude,
    OutputMustNotInclude,
    PathForbidden,
    ToolForbidden,
    load_ledger_yaml,
)

EXTRACT_PROMPT = """\
Below is a text written to a team of AI agents named: {agents}. List every rule the agents \
must follow that the text states: something they must or must not do, or must ask first. A \
rule is often given with its reason ("X is locked, so ..."): it is still a rule. Do not list \
the task itself (what to produce), and do not list preferences about length, tone, style or \
format unless the text says they are required. For each rule give:
- text: the whole sentence that states the rule, quoted from the text, including the files, \
folders, tools or reasons it names;
- kind: "constraint" for a must or must-not, "scope" for a limit on what to work on, "fact" \
for a stated fact the agents must not contradict;
- source: "user" if the person asking states it, otherwise "system";
- authority: "binding" if it must be obeyed, "advisory" if it is a preference;
- scope: the names of the agents (from the list above) that could break it.
Return an empty list if the text states no rule.

Text:
{text}"""

COMPILE_PROMPT = """\
Express the rule below as exactly one check from this closed list, or answer type "none" if \
no single check expresses it exactly. Use only these agents: {agents}; and only these tools: \
{tools}. File paths and globs are relative to the repository root.
- path_forbidden: glob, ops (any of "write", "delete") — no write or delete under the glob
- files_frozen: globs — none of these files may change
- tool_forbidden: name — the tool must never be called
- approval_before: tool — the tool may only be called after approval was asked
- output_must_include: regex, agent — the agent's reply must contain a match
- output_must_not_include: regex, agent — the agent's reply must not contain a match
Fill only the fields of the check you choose.

Choose by what the rule protects:
- A folder whose files must not be created, changed or removed: path_forbidden with glob \
"<folder>/**" and ops ["write", "delete"].
- One named file whose contents must stay as they are, including a file that lists or pins \
what may be used (a rule against adding or changing what it lists is about that file): \
files_frozen with that file's path. files_frozen is for named files only, never for a folder.
- An action that must not happen at all: tool_forbidden on the tool that performs it. Pick \
the tool by what it does; the rule need not name it. Use tool_forbidden only if every call of \
that tool would break the rule, never for a general tool such as one that writes or edits files.
- An action allowed only after asking: approval_before on the tool that performs it.
- What an agent's reply, or what it hands back, must or must not say (a line it must contain, \
a word or name it must not mention): output_must_include or output_must_not_include for that \
agent, with the shortest regex that matches exactly what the rule requires: the literal words, \
escaped, and for a line that starts with some text, (?m)^ followed by that text and nothing \
after it.

The rule comes from the text below. Use the text only to tell which files, folders, tools or \
agents the rule means; do not make checks from other parts of it.

Rule:
{rule}

Text:
{text}"""

_MODELS = {
    "path_forbidden": PathForbidden,
    "files_frozen": FilesFrozen,
    "tool_forbidden": ToolForbidden,
    "approval_before": ApprovalBefore,
    "output_must_include": OutputMustInclude,
    "output_must_not_include": OutputMustNotInclude,
}
_CHECK = TypeAdapter(Check)


class ProposedRule(BaseModel):
    text: str
    kind: Literal["constraint", "scope", "fact"]
    source: Literal["user", "system", "tool", "agent_inference"]
    authority: Literal["binding", "advisory"]
    scope: list[str]


class Proposals(BaseModel):
    rules: list[ProposedRule]


class CompiledCheck(BaseModel):
    """Flat on purpose: one optional field per DSL argument, validated against Check after."""

    type: Literal[
        "path_forbidden",
        "files_frozen",
        "tool_forbidden",
        "approval_before",
        "output_must_include",
        "output_must_not_include",
        "none",
    ]
    glob: str | None = None
    ops: list[Literal["write", "delete"]] | None = None
    globs: list[str] | None = None
    name: str | None = None
    tool: str | None = None
    regex: str | None = None
    agent: str | None = None


@dataclass(frozen=True)
class Proposal:
    rule: ProposedRule
    check: Check | None


def extract_rules(model: Any, text: str, agents: Sequence[str]) -> list[ProposedRule]:
    """The helper's proposals, with any agent the team does not have dropped from each scope."""
    prompt = EXTRACT_PROMPT.format(agents=", ".join(agents), text=text)
    rules = model.with_structured_output(Proposals).invoke(prompt).rules
    known = set(agents)
    return [r.model_copy(update={"scope": [a for a in r.scope if a in known]}) for r in rules]


# An unescaped trailing `.*` (or `.*$`); a backslash before it means a literal dot, left alone.
_TRAILING_ANY = re.compile(r"(?<!\\)(?:\.\*)+$")
_TRAILING_ANY_TO_EOL = re.compile(r"(?<!\\)(?:\.\*)+\$$")


def _canonical(data: dict[str, Any]) -> dict[str, Any]:
    """The same check in one spelling, by rewrites that never change what it matches under the
    oracle (re.search for regexes, oracle.paths.glob_matches for globs):
    - a regex's trailing `.*` matches the empty string, so search finds the same texts without
      it; a trailing `.*$` is dropped too when the regex is multiline (`(?m)`), where `$` always
      matches at the end of the line `.*` stops at;
    - a glob's trailing `/*` is `/**`: both `*` and `**` cross `/` in glob_matches.
    """
    out = dict(data)
    regex = out.get("regex")
    if isinstance(regex, str):
        if regex.startswith("(?m)"):
            regex = _TRAILING_ANY_TO_EOL.sub("", regex)
        out["regex"] = _TRAILING_ANY.sub("", regex) or data["regex"]
    if isinstance(out.get("glob"), str):
        out["glob"] = _star_dir(out["glob"])
    if isinstance(out.get("globs"), list):
        out["globs"] = [_star_dir(g) if isinstance(g, str) else g for g in out["globs"]]
    return out


def _star_dir(glob: str) -> str:
    return glob[:-1] + "**" if glob.endswith("/*") and not glob.endswith("/**") else glob


def compile_rule(
    model: Any, rule: str, agents: Sequence[str], tools: Sequence[str], text: str = ""
) -> Check | None:
    """One closed-list check for `rule`, or None when it is not expressible, does not validate,
    or names a tool or agent the app does not have (such a check could never fire)."""
    prompt = COMPILE_PROMPT.format(
        rule=rule, agents=", ".join(agents), tools=", ".join(tools), text=text or rule
    )
    reply = model.with_structured_output(CompiledCheck).invoke(prompt)
    if reply.type == "none":
        return None
    fields = _MODELS[reply.type].model_fields
    data = _canonical(
        {k: v for k, v in reply.model_dump().items() if k in fields and v is not None}
    )
    named_tool = data.get("name", data.get("tool"))
    if (named_tool is not None and named_tool not in tools) or (
        "agent" in data and data["agent"] not in agents
    ):
        return None
    try:
        return _CHECK.validate_python(data)
    except ValidationError:
        return None


def propose(model: Any, text: str, agents: Sequence[str], tools: Sequence[str]) -> list[Proposal]:
    rules = extract_rules(model, text, agents)
    return [Proposal(rule, compile_rule(model, rule.text, agents, tools, text)) for rule in rules]


def to_constraint(rule: ProposedRule, check: Check | None, constraint_id: str) -> Constraint:
    """The ledger entry for a confirmed proposal; an inconsistent check leaves it unverified."""
    data = {**rule.model_dump(), "id": constraint_id, "origin": "extracted"}
    try:
        return Constraint.model_validate({**data, "check": check})
    except ValidationError:
        if check is None:
            raise
        return Constraint.model_validate({**data, "check": None})


def next_ids(ledger: Ledger, n: int) -> list[str]:
    top = max((int(c.id.split("-")[1]) for c in ledger.constraints), default=0)
    return [f"C-{top + i:03d}" for i in range(1, n + 1)]


def append_constraints(path: str | Path, new: Sequence[Constraint]) -> Ledger:
    """Append `new` to the ledger file without rewriting a byte of what is there.

    The combined ledger is validated before the file is touched, and the written file is
    reloaded and compared; on any mismatch the original text is put back and ValueError raised.
    """
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    current = load_ledger_yaml(original).model_dump(mode="json")
    entries = [c.model_dump(mode="json") for c in new]
    expected = Ledger.model_validate(
        {**current, "constraints": [*current["constraints"], *entries]}
    )
    found = re.search(r"^([ \t]*)- id:", original, re.MULTILINE)
    indent = found.group(1) if found else "  "
    items = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, default_flow_style=False)
    block = "".join(f"{indent}{line}\n" for line in items.splitlines())
    path.write_text(original + ("" if original.endswith("\n") else "\n") + block, "utf-8")
    try:
        written = load_ledger_yaml(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - any failure to reload means the append went wrong
        written = None
    if written != expected:
        path.write_text(original, encoding="utf-8")
        raise ValueError(
            f"{path}: appending did not produce the expected ledger; the constraints list must "
            "be the last key in the file (the file was left unchanged)"
        )
    return expected


def _show(constraint: Constraint) -> str:
    return yaml.safe_dump([constraint.model_dump(mode="json")], sort_keys=False).rstrip()


def main(
    argv: Sequence[str] | None = None,
    *,
    model: Any = None,
    ask: Callable[[str], str] = input,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m ledger.extract", description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--text-file", required=True, type=Path)
    parser.add_argument("--agents", required=True, help="comma-separated agent names")
    parser.add_argument("--tools", required=True, help="comma-separated tool names")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="show proposals, write nothing")
    group.add_argument("--yes", action="store_true", help="accept every proposal")
    args = parser.parse_args(argv)

    if model is None:
        if not is_live():
            print(
                "refusing: extraction calls the helper model; set NEMOGATE_LIVE=1", file=sys.stderr
            )
            return 2
        from common.llm import get_model

        model = get_model(Role.helper)
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    text = args.text_file.read_text(encoding="utf-8")
    ledger = load_ledger_yaml(args.ledger.read_text(encoding="utf-8"))
    proposals = propose(model, text, agents, tools)
    print(f"{len(proposals)} proposed rule(s)")

    accepted: list[Constraint] = []
    for proposal in proposals:
        (constraint_id,) = next_ids(
            ledger.model_copy(update={"constraints": (*ledger.constraints, *accepted)}), 1
        )
        try:
            constraint = to_constraint(proposal.rule, proposal.check, constraint_id)
        except ValidationError as exc:
            reason = exc.errors()[0]["msg"]
            print(f"skipped (not a valid ledger entry): {proposal.rule.text!r}: {reason}")
            continue
        print(_show(constraint))
        if args.dry_run:
            continue
        if args.yes or ask("Add it to the ledger? [y/N] ").strip().lower() in ("y", "yes"):
            accepted.append(constraint)
    if accepted and not args.dry_run:
        append_constraints(args.ledger, accepted)
        print(f"appended {', '.join(c.id for c in accepted)} to {args.ledger}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
