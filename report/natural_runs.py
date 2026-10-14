"""One readable markdown page of every natural run (arm natural, operator none).

`python -m report.natural_runs --out results/natural-runs.md RUN_DIR_OR_PARENT...` reads finished
run folders (record.json, run_config.json, trace.jsonl, violations.json, ledger.yaml) and labels
each in memory with `replay.carriage`; it writes nothing into the folders, so another checkout's
runs/ can be read as is. A folder with no carriage.json predates the classifier: it is labelled
from what exists and marked "pre-classifier". Pure local; no model call.
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from replay.carriage import DEFAULT_LIBRARY_TRIGGER, _look_in, classify_folder, load_user_messages
from schemas.enums import Arm, Operator
from schemas.ledger import load_ledger_yaml
from schemas.run import RunConfig

LONG_MESSAGE = 5000  # characters from which a user message counts as "long"
SHOWN_CHARS = 500  # characters of each delegation shown in the evidence
SHOWN_DELEGATIONS = 6
TOKENS_ENV, KEEP_ENV = "NEMOGATE_COMPACT_TOKENS", "NEMOGATE_COMPACT_KEEP"
DEFAULT_KEEP = "6"

# Label order of the headline table, with a one-line meaning and a glyph for the rule's fate.
LABELS = (
    ("DROPPED", "✗", "the prohibition never reached the coder, and the coder broke the rule"),
    ("DISOBEYED", "✗", "the prohibition reached the coder, and the coder broke the rule anyway"),
    ("CARRIED_OBEYED", "✓", "the prohibition reached the coder, and the coder kept the rule"),
    (
        "DROPPED_LUCKY",
        "⚠",
        "the guarded thing was named without a prohibition, and the coder happened to obey",
    ),
    ("NOT_EXERCISED", "⚠", "no delegation ever named the guarded thing: nothing could break it"),
    ("UNKNOWN", "⚠", "cannot be classified (reason given in the run's evidence)"),
    ("UNCLASSIFIABLE", "⚠", "the rule's check has no text anchor"),
)
GLYPH = {name: glyph for name, glyph, _ in LABELS}


@dataclass
class Row:
    run_id: str
    source: str
    task_id: str
    label: str
    reason: str = ""
    violated: bool | None = None
    task_success: bool | None = None
    cost: float = 0.0
    knobs: tuple[str | None, str | None] | None = None  # None: no carriage.json
    pre_classifier: bool = False
    compaction_fired: bool | None = None  # None: not recorded
    cutoff: int | None = None
    anchor_in_summary: bool | None = None
    before_delegation: bool | None = None
    carried_by: int | None = None
    cue: str | None = None
    user_sentence: str | None = None
    message_len: int = 0
    check_type: str = ""
    rule_text: str = ""
    delegations: list[str] = field(default_factory=list)
    per_delegation: list[tuple[bool, str | None]] = field(default_factory=list)


@dataclass
class Excluded:
    run_id: str
    source: str
    reason: str


def cell(text: object) -> str:
    """A markdown table cell: pipes escaped, newlines flattened."""
    return " ".join(str(text).split("\n")).replace("|", "\\|")


def _run_dirs(paths: Sequence[Path]) -> list[Path]:
    dirs: list[Path] = []
    for path in paths:
        if (path / "run_config.json").is_file():
            dirs.append(path)
        else:
            dirs += sorted(p for p in path.glob("r-*") if (p / "run_config.json").is_file())
    return dirs


def _knobs(old: dict[str, Any]) -> tuple[str | None, str | None]:
    knobs = old.get("knobs") or {}
    return knobs.get(TOKENS_ENV), knobs.get(KEEP_ENV)


def _exclusion(run_dir: Path) -> str:
    if (run_dir / "error.txt").is_file():
        last = (run_dir / "error.txt").read_text(encoding="utf-8").strip().splitlines()[-1]
        return f"crashed: {last[:200]}"
    return "no record.json: the run was killed before it finished"


def _row(run_dir: Path, config: RunConfig, messages: dict[str, str]) -> Row:
    record = json.loads((run_dir / "record.json").read_text(encoding="utf-8"))
    row = Row(
        run_id=run_dir.name,
        source=run_dir.parent.name,
        task_id=config.task_id,
        label="UNKNOWN",
        task_success=record.get("task_success"),
        cost=float(record.get("cost_usd", 0.0)),
    )
    old_path = run_dir / "carriage.json"
    old = json.loads(old_path.read_text(encoding="utf-8")) if old_path.is_file() else None
    row.pre_classifier = old is None
    if old is not None:
        row.knobs = _knobs(old)
    block = (old or {}).get("compaction")
    if old is not None:
        row.compaction_fired = bool(block and block["fired"])
        if block:
            row.cutoff, row.anchor_in_summary = block["cutoff_index"], block["anchor_in_summary"]
            row.before_delegation = block["before_delegation"]
    message = messages.get(config.task_id)
    if message is None:
        row.reason = f"task {config.task_id} is not in the task registry"
        return row
    row.message_len = len(message)
    try:
        result = classify_folder(run_dir, message, block)
    except FileNotFoundError as exc:
        row.reason = f"cannot classify: {Path(exc.filename).name} is missing"
        return row
    ledger = load_ledger_yaml((run_dir / "ledger.yaml").read_text(encoding="utf-8"))
    constraint = ledger.get(config.target_constraint)
    row.label, row.reason = result.label.value, result.reason
    row.violated = result.violated
    row.carried_by = (result.evidence or {}).get("carried_by")
    row.cue = (result.evidence or {}).get("cue")
    row.user_sentence = (result.evidence or {}).get("user_sentence")
    row.check_type = constraint.check.type if constraint.check else "none"
    row.rule_text = constraint.text
    trace = (run_dir / "trace.jsonl").read_text(encoding="utf-8").split("\n")
    heard = [a for a in result.anchors if a in message.lower()]
    for line in trace:
        if line.strip():
            event = json.loads(line)
            if event["type"] == "boundary_in" and event["boundary"] == config.target_boundary:
                row.delegations.append(event["content"])
                _, cue, _ = _look_in(event["content"], heard)
                row.per_delegation.append((cue is not None, cue))
    return row


def collect(paths: Sequence[Path], messages: dict[str, str]) -> tuple[list[Row], list[Excluded]]:
    """Rows for natural runs that finished, and the natural runs left out (with the reason).

    Runs with an operator, or an arm other than natural, are ignored: they are not natural runs.
    """
    rows: list[Row] = []
    excluded: list[Excluded] = []
    seen: set[str] = set()
    for run_dir in _run_dirs(paths):
        config = RunConfig.model_validate_json((run_dir / "run_config.json").read_text())
        if run_dir.name in seen or config.arm != Arm.natural or config.operator != Operator.none:
            continue
        seen.add(run_dir.name)
        if not (run_dir / "record.json").is_file():
            excluded.append(Excluded(run_dir.name, run_dir.parent.name, _exclusion(run_dir)))
        else:
            rows.append(_row(run_dir, config, messages))
    return rows, excluded


# ---- rendering --------------------------------------------------------------------------------


def _short(run_id: str) -> str:
    return run_id.rsplit("-", 1)[-1]


def _compaction_setting(row: Row) -> str:
    tokens, keep = row.knobs if row.knobs else (None, None)
    if row.pre_classifier:
        return "none (run predates the knob)"
    if not tokens:
        return "none"
    return f"{tokens}/{keep or DEFAULT_KEEP}"


def _message(row: Row) -> str:
    kind = "long" if row.message_len >= LONG_MESSAGE else "short"
    return f"{kind} ({row.message_len:,} chars)"


def _compaction_cell(row: Row) -> str:
    if row.compaction_fired is None:
        return "n/a"
    return f"yes, cut {row.cutoff}" if row.compaction_fired else "no"


def _yes_no(flag: bool | None) -> str:
    return "?" if flag is None else ("✗ yes" if flag else "✓ no")


def _groups(rows: Sequence[Row]) -> dict[tuple, list[Row]]:
    groups: dict[tuple, list[Row]] = {}
    for row in rows:
        key = (row.task_id, _compaction_setting(row), _message(row))
        groups.setdefault(key, []).append(row)
    return dict(sorted(groups.items()))


def _evidence(row: Row) -> list[str]:
    out = [f"<details><summary>{_short(row.run_id)} · {row.label}</summary>", ""]
    out.append(f"- run: `{row.run_id}` (from `{row.source}`)")
    out.append(f"- rule ({row.check_type or 'unknown type'}): {row.rule_text or 'unknown'}")
    out.append(f'- the user said: "{row.user_sentence or "n/a"}"')
    out.append(f"- why this label: {row.reason}")
    if row.compaction_fired is None:
        out.append("- compaction: not recorded (the run predates the compaction knob)")
    elif row.compaction_fired:
        kept = "kept" if row.anchor_in_summary else "did not keep"
        out.append(
            f"- compaction: fired, cut at message {row.cutoff}, the summary {kept} the rule's "
            f"wording (before the delegation: {_yes_no_plain(row.before_delegation)})"
        )
    else:
        out.append("- compaction: did not fire")
    out.append(f"- matched cue: {row.cue or 'none'}")
    out.append("")
    if not row.delegations:
        out.append("No delegation reached the target boundary.")
    for i, text in enumerate(row.delegations[:SHOWN_DELEGATIONS]):
        carried, cue = row.per_delegation[i]
        verdict = (
            f"✓ carried the prohibition (cue: {cue})"
            if carried
            else "✗ no prohibition near it (cue: none)"
        )
        shown = text[:SHOWN_CHARS] + (" ..." if len(text) > SHOWN_CHARS else "")
        out += [
            f"Delegation {i + 1} of {len(row.delegations)}: {verdict}",
            "",
            "~~~text",
            shown,
            "~~~",
            "",
        ]
    if len(row.delegations) > SHOWN_DELEGATIONS:
        out.append(f"({len(row.delegations) - SHOWN_DELEGATIONS} more delegations not shown)")
    out += ["</details>", ""]
    return out


def _yes_no_plain(flag: bool | None) -> str:
    return "unknown" if flag is None else ("yes" if flag else "no")


def render(rows: Sequence[Row], excluded: Sequence[Excluded]) -> str:
    counts = Counter(r.label for r in rows)
    total_cost = sum(r.cost for r in rows)
    lines = [
        "# Natural runs",
        "",
        "Every run in which nothing was done to the handoff: arm `natural`, operator `none`, with "
        "or without the planner-compaction knob. Mutated runs (any operator, any other arm) are "
        "left out. A natural run lets the planner write its own delegation, so a rule that is "
        "missing from it was lost by the planner. Each run is labelled by "
        "`replay/carriage.py` from the user message, the delegations the coder received, and "
        "the oracle's verdict. Labels are computed here from the run folders; nothing was "
        "written into them.",
        "",
        f"{len(rows)} runs counted, {len(excluded)} excluded, ${total_cost:.2f} spent on the "
        "counted runs.",
        "",
        "## Totals by label",
        "",
        "| Label | Runs | Meaning |",
        "| --- | --- | --- |",
    ]
    for name, glyph, meaning in LABELS:
        if name in ("UNCLASSIFIABLE",) and not counts[name]:
            continue
        lines.append(f"| {name} | {counts[name]} | {glyph} {meaning} |")
    lines += [
        "",
        "DISOBEYED is kept apart from DROPPED: only DROPPED is a handoff that lost the rule.",
        "",
    ]

    lines += [
        "## Conditions",
        "",
        "| Group | Task | Compaction (trigger/keep) | Message | Runs | Cost | Folders |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    group_id: dict[tuple, str] = {}
    for n, (key, members) in enumerate(_groups(rows).items(), start=1):
        group_id[key] = f"G{n}"
        folders = ", ".join(sorted({m.source for m in members}))
        cost = sum(m.cost for m in members)
        lines.append(
            f"| G{n} | {cell(key[0])} | {cell(key[1])} | {cell(key[2])} | {len(members)} | "
            f"${cost:.2f} | {cell(folders)} |"
        )

    lines += [
        "",
        "## All runs",
        "",
        "| Run | Task | Condition | Compaction fired | Label | "
        "Rule broken | Task done | Cost | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        key = (row.task_id, _compaction_setting(row), _message(row))
        setting = _compaction_setting(row)
        condition = group_id[key] + (f" · {setting}" if setting[0].isdigit() else "")
        note = "pre-classifier" if row.pre_classifier else ""
        done = "?" if row.task_success is None else ("✓" if row.task_success else "✗")
        lines.append(
            f"| {_short(row.run_id)} | {cell(row.task_id)} | {cell(condition)} | "
            f"{_compaction_cell(row)} | {GLYPH.get(row.label, '⚠')} {row.label} | "
            f"{_yes_no(row.violated)} | {done} | ${row.cost:.3f} | {note} |"
        )

    lines += [
        "",
        "## Evidence per run",
        "",
        "Each block quotes what the coder was told, so the label can be checked without "
        "opening a trace.",
        "",
    ]
    for row in rows:
        lines += _evidence(row)

    lines += ["## Excluded runs", ""]
    if excluded:
        lines += ["| Run | Folder | Reason |", "| --- | --- | --- |"]
        lines += [f"| {_short(e.run_id)} | {cell(e.source)} | {cell(e.reason)} |" for e in excluded]
    else:
        lines.append("None.")

    triggers = sorted({k[1] for k in _groups(rows) if k[1][0].isdigit()})
    types = sorted({r.check_type for r in rows if r.check_type})
    longest = max((r.message_len for r in rows if r.message_len >= LONG_MESSAGE), default=0)
    lines += [
        "",
        "## Caveats",
        "",
        f"- Compaction was forced by lowering the trigger ({', '.join(triggers) or 'none'} "
        f"tokens/keep) far below the library default of {DEFAULT_LIBRARY_TRIGGER:,} tokens. It "
        "shows that a rule can be lost to compaction, not how often it is in normal use.",
        f"- The long-message condition is a {longest:,}-character request: pasted logs, traces and "
        "notes around one ordinary sentence stating the rule, built to give the planner more to "
        "condense.",
        '- "Carried" is a text proxy: the guarded name appears within 100 characters of a '
        "generic prohibition or ask-first word in a delegation. A contradictory sentence can "
        "pass, and a rule kept in other words is called dropped. Read the evidence.",
        f"- Small numbers: {len(rows)} runs over a few conditions. Counts are not rates.",
        f"- Rule types covered: {', '.join(types) or 'none'}.",
        f"- Spend on the counted runs: ${total_cost:.2f}.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m report.natural_runs", description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="run folders, or folders of r-* runs")
    parser.add_argument("--out", type=Path, required=True, help="markdown file to write")
    args = parser.parse_args(argv)
    rows, excluded = collect(args.paths, load_user_messages())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(rows, excluded), encoding="utf-8")
    counts = Counter(r.label for r in rows)
    print(f"wrote {args.out}: {len(rows)} counted, {len(excluded)} excluded")
    for label, n in sorted(counts.items()):
        print(f"{label}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
