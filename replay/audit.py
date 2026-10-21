"""Offline audit of saved runs: score them again from their own files.

`uv run python -m replay.audit RUN_DIR [RUN_DIR ...]` reads each run folder's ledger, trace (a
fork's prefix first) and file diff, calls the oracle, and says whether the rules it finds broken
are the ones the run recorded. The contract is in replay/README.md, "Auditing a saved run".
It reads files only: no key, no network, no model, no sandbox.
"""

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from oracle import evaluate
from replay import record
from replay.carriage import check_trace, fork_scored, fork_trace
from schemas.ledger import load_ledger_yaml
from schemas.run import FS_DIFF, RunRecord, Violation
from schemas.trace import parse_trace_jsonl

RECORD = "record.json"
FILES = (RECORD, "trace.jsonl", "fs_diff.json", "ledger.yaml", "violations.json")
FORK_FILES = ("fork.json", "prefix.jsonl")  # a fork has both
INHERITED = "inherited_violations.json"  # a fork's: what the source did before the fork point
HEADER = ("RUN", "TASK", "ARM", "OPERATOR", "RULE", "RECORDED", "RECOMPUTED", "HASH", "MATCH")
VIOLATIONS = TypeAdapter(list[Violation])

DESCRIPTION = """\
Check the scoring of saved runs. For each run folder (or each run folder inside a folder you
name) this reads the saved ledger, trace and file diff, scores them again with the oracle, and
compares the rules it finds broken with the ones the run recorded (record.json and
violations.json). It also checks that ledger.yaml still hashes to the ledger hash in record.json.
It needs no API key, no network, no model and no sandbox. It checks the scoring, not the model:
it does not run any agent again, so it says nothing about whether the agents would behave the
same way twice. A fork is scored on its own events after the fork point, from the file diff of
the whole run (fs_diff.json), as its record was."""
EPILOG = """\
exit code: 0 every run matches and every hash checks; 1 a run differs; 2 a folder could not be
read (the message names the folder and the file)."""


class Unreadable(Exception):
    """A run folder that cannot be audited; the message names the folder and the file."""


@dataclass(frozen=True)
class Audit:
    run_id: str
    task: str
    arm: str
    operator: str
    rule: str  # the run's target rule id
    recorded_ids: tuple[str, ...]  # record.json
    violations_file_ids: tuple[str, ...]  # violations.json
    recomputed_ids: tuple[str, ...]
    ledger_hash: str  # of ledger.yaml now
    recorded_ledger_hash: str  # in record.json

    @property
    def short(self) -> str:
        return self.run_id.rsplit("-", 1)[-1]

    @property
    def hash_ok(self) -> bool:
        return self.ledger_hash == self.recorded_ledger_hash

    @property
    def ids_agree(self) -> bool:
        return self.recorded_ids == self.violations_file_ids == self.recomputed_ids

    @property
    def match(self) -> bool:
        return self.ids_agree and self.hash_ok

    def verdict(self, ids: tuple[str, ...]) -> str:
        return "broke" if self.rule in ids else "obeyed"

    def notes(self) -> list[str]:
        """Why a run differs, in rule ids and hashes only."""
        notes = []
        if not self.ids_agree:
            notes.append(
                f"differs: {self.short}: record.json {_ids(self.recorded_ids)}, "
                f"violations.json {_ids(self.violations_file_ids)}, "
                f"recomputed {_ids(self.recomputed_ids)}"
            )
        if not self.hash_ok:
            notes.append(
                f"differs: {self.short}: ledger.yaml hashes to {self.ledger_hash}, "
                f"record.json carries {self.recorded_ledger_hash}"
            )
        return notes

    def row(self) -> tuple[str, ...]:
        return (
            self.short,
            self.task,
            self.arm,
            self.operator,
            self.rule,
            self.verdict(self.recorded_ids),
            self.verdict(self.recomputed_ids),
            "ok" if self.hash_ok else "BAD",
            "yes" if self.match else "NO",
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "arm": self.arm,
            "operator": self.operator,
            "rule": self.rule,
            "recorded": self.verdict(self.recorded_ids),
            "recomputed": self.verdict(self.recomputed_ids),
            "hash": "ok" if self.hash_ok else "BAD",
            "match": self.match,
            "recorded_ids": list(self.recorded_ids),
            "violations_file_ids": list(self.violations_file_ids),
            "recomputed_ids": list(self.recomputed_ids),
        }


def _ids(ids: Iterable[str]) -> str:
    return ", ".join(ids) or "none"


def _read(run_dir: Path, name: str, parse: Callable[[str], Any]) -> Any:
    path = run_dir / name
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else None
    except OSError as exc:
        raise Unreadable(f"{run_dir}: cannot read {name}: {_reason(exc)}") from exc
    if text is None:
        raise Unreadable(f"{run_dir}: missing {name}")
    try:
        return parse(text)
    except Exception as exc:  # a damaged file is the user's to fix: name it, never a traceback
        raise Unreadable(f"{run_dir}: cannot read {name}: {_reason(exc)}") from exc


def _reason(exc: Exception) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0]


def audit_run(run_dir: Path) -> Audit:
    """Score one run folder again from its files. `Unreadable` if one is missing or damaged."""
    saved = _read(run_dir, RECORD, RunRecord.model_validate_json)
    ledger = _read(run_dir, "ledger.yaml", load_ledger_yaml)
    trace = _read(run_dir, "trace.jsonl", parse_trace_jsonl)
    try:
        check_trace(
            trace,
            run_dir / "trace.jsonl",
            run_id=saved.run_id,
            ledger_hash=saved.ledger_hash,
            safe_config_hash=saved.safe_config_hash,
        )
    except RuntimeError as exc:  # the runner's own check: an empty or foreign trace is no evidence
        raise Unreadable(str(exc)) from None
    fs_diff = _read(run_dir, "fs_diff.json", FS_DIFF.validate_json)
    in_file = _read(run_dir, "violations.json", VIOLATIONS.validate_json)
    try:
        scoped = record.scoped_ledger(ledger, saved.constraint_id)
    except KeyError:
        raise Unreadable(
            f"{run_dir}: ledger.yaml has no rule {saved.constraint_id}, the run's target"
        ) from None

    is_fork = any((run_dir / name).exists() for name in FORK_FILES)
    if is_fork:
        for name in FORK_FILES:
            if not (run_dir / name).is_file():
                raise Unreadable(f"{run_dir}: missing {name}")
        prefix = _read(run_dir, "prefix.jsonl", parse_trace_jsonl)
        trace = fork_trace(prefix, trace)  # the trace the runner scored, steps and all
    found = evaluate(scoped, trace, fs_diff)
    if is_fork:
        # The runner scored a fork on the file diff from the fork-point image, which is not saved;
        # fs_diff.json is from the base image. A violation of a change the fork point already
        # held was set aside there as inherited, so it is set aside here the same way.
        inherited = (
            _read(run_dir, INHERITED, VIOLATIONS.validate_json)
            if (run_dir / INHERITED).is_file()
            else []
        )
        found = [v for v in fork_scored(found, len(prefix)) if v not in inherited]

    return Audit(
        run_id=saved.run_id,
        task=saved.task_id,
        arm=saved.arm.value,
        operator=saved.operator.value,
        rule=saved.constraint_id,
        recorded_ids=tuple(sorted(set(saved.violations))),
        violations_file_ids=tuple(sorted({v.constraint_id for v in in_file})),
        recomputed_ids=tuple(sorted({v.constraint_id for v in found})),
        ledger_hash=record.ledger_hash(ledger),
        recorded_ledger_hash=saved.ledger_hash,
    )


def run_folders(paths: Iterable[Path]) -> list[Path]:
    """Each path that holds a record.json, or else its child folders that do (sorted by name)."""
    folders: list[Path] = []
    for path in paths:
        if not path.exists():
            raise Unreadable(f"{path}: no such folder")
        if not path.is_dir():
            raise Unreadable(f"{path}: not a folder")
        try:
            if (path / RECORD).is_file():
                found = [path]
            else:
                found = sorted(
                    (c for c in path.iterdir() if (c / RECORD).is_file()), key=lambda c: c.name
                )
        except OSError as exc:
            raise Unreadable(f"{path}: cannot read folder: {_reason(exc)}") from exc
        if not found:
            raise Unreadable(f"{path}: missing {RECORD}")
        folders += [f for f in found if f not in folders]
    return folders


def render_table(audits: Sequence[Audit]) -> str:
    rows = [HEADER, *(a.row() for a in audits)]
    widths = [max(len(row[i]) for row in rows) for i in range(len(HEADER))]
    return "\n".join(
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in rows
    )


def summary(audits: Sequence[Audit]) -> dict[str, int]:
    matched = sum(a.match for a in audits)
    return {"runs": len(audits), "match": matched, "differ": len(audits) - matched}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m replay.audit", description=DESCRIPTION, epilog=EPILOG
    )
    parser.add_argument("run_dirs", nargs="+", type=Path, metavar="RUN_DIR")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)

    audits: list[Audit] = []
    errors: list[str] = []
    try:
        folders = run_folders(args.run_dirs)
    except Unreadable as exc:
        folders, errors = [], [str(exc)]
    for folder in folders:
        try:
            audits.append(audit_run(folder))
        except Unreadable as exc:
            errors.append(str(exc))
        except OSError as exc:  # a folder that went unreadable mid-audit, whatever the file
            errors.append(f"{folder}: cannot read folder: {_reason(exc)}")
    if errors:
        print("\n".join(f"audit: {e}" for e in errors), file=sys.stderr)
        return 2

    total = summary(audits)
    if args.json:
        data = {"runs": [a.as_json() for a in audits], "summary": total}
        print(json.dumps(data, indent=2))
    else:
        notes = [n for a in audits for n in a.notes()]
        line = f"{total['runs']} runs, {total['match']} match, {total['differ']} differ"
        print("\n".join([render_table(audits), *notes, "", line]))
    return 0 if total["differ"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
