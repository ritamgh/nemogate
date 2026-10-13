"""What the repair planner sees about a lost rule, read from run folders written by the runner.

Plain data: counts and one example of the handoff as sent and as received. Nothing here grades.
"""

from dataclasses import dataclass
from pathlib import Path

from schemas.ledger import load_ledger_yaml
from schemas.run import RunRecord
from schemas.trace import BoundaryIn, BoundaryOut, parse_trace_jsonl


@dataclass(frozen=True)
class Evidence:
    constraint_id: str
    rule: str
    check_type: str
    boundary: str
    runs: int
    violated: int
    example_sent: str
    example_received: str


def load_records(
    runs_dir: str | Path,
    constraint_id: str,
    task_id: str | None = None,
    *,
    arm: str | None = None,
    operator: str | None = None,
) -> list[tuple[Path, RunRecord]]:
    """Every run folder under `runs_dir` whose record targets `constraint_id`, optionally only
    for one task, arm and operator (a baseline must compare like with like)."""
    found = []
    for folder in sorted(Path(runs_dir).iterdir()):
        path = folder / "record.json"
        if not path.is_file():
            continue
        record = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if (
            record.constraint_id == constraint_id
            and task_id in (None, record.task_id)
            and arm in (None, record.arm.value)
            and operator in (None, record.operator.value)
        ):
            found.append((folder, record))
    return found


def _example(folder: Path, boundary: str) -> tuple[str, str]:
    trace_path = folder / "trace.jsonl"
    if not trace_path.is_file():
        return "", ""
    trace = parse_trace_jsonl(trace_path.read_text(encoding="utf-8"))
    sent = next(
        (e.content for e in trace if isinstance(e, BoundaryOut) and e.boundary == boundary), ""
    )
    got = next(
        (e.content for e in trace if isinstance(e, BoundaryIn) and e.boundary == boundary), ""
    )
    return sent, got


def load_evidence(runs_dir: str | Path, constraint_id: str, task_id: str | None = None) -> Evidence:
    runs = load_records(runs_dir, constraint_id, task_id)
    if not runs:
        raise ValueError(f"no runs in {runs_dir} target {constraint_id}")
    violated = [(f, r) for f, r in runs if constraint_id in r.violations]
    folder, sample = (violated or runs)[0]
    ledger = load_ledger_yaml((folder / "ledger.yaml").read_text(encoding="utf-8"))
    constraint = ledger.get(constraint_id)
    sent, got = _example(folder, sample.boundary)
    return Evidence(
        constraint_id=constraint_id,
        rule=constraint.text,
        check_type=constraint.check.type if constraint.check else "unverified",
        boundary=sample.boundary,
        runs=len(runs),
        violated=len(violated),
        example_sent=sent,
        example_received=got,
    )
