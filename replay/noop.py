"""Runs whose mutation changed nothing: counted as `mutation_noop`, never pooled, never retried.

A `drop` or `hedge` that finds no sentence carrying the rule (the planner already left it out)
is correct behaviour, not a failure, but the run is no sample of the mutation. Two ways to see it:

- Typed: the gate raises `NothingToMutate` while the agents run, the runner writes `error.json`
  with that class as `error_type`, and `is_noop_error` recognises it. The name is derived from the
  class, so a rename in `gate` fails a test instead of turning noops into `run_error`.
- Text: a run that finished under an operator but whose receiver got the sender's text at the
  target boundary. This catches `compress_*`, which has no no-op check in the gate.

For the text check, the sender's text is `boundary_out.content` and what the receiver got is
`boundary_in.content` (the gate writes the operator's output, plus on a delegation under the gated
arm its pinned-rules block, which `unchanged_at_target` strips with the gate's own format).
"""

from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from gate import NothingToMutate
from gate.middleware import REPIN
from schemas.enums import Arm, Operator
from schemas.run import RunRecord
from schemas.trace import BoundaryIn, BoundaryOut, Event, parse_trace_jsonl

NOOP_ERROR_TYPE = f"{NothingToMutate.__module__}.{NothingToMutate.__qualname__}"
# What the gate puts in front of the pinned rules; the rules after it are not needed to cut it.
_PIN_MARK = REPIN.partition("{rules}")[0]


def is_noop_error(error_type: object) -> bool:
    return error_type == NOOP_ERROR_TYPE


def unchanged_at_target(trace: Sequence[Event], boundary: str, *, gated: bool) -> bool:
    """True if every handoff at `boundary` reached the receiver as the sender sent it (compared
    without surrounding whitespace, as the gate's own check does), and there was at least one.

    The trace has no key that ties a `boundary_in` to its `boundary_out`, and concurrent handoffs
    interleave their events, so the payloads are compared as multisets: unchanged means the
    received texts are the sent texts, each as often as it was sent. `gated` runs append a
    pinned-rules block to a delegation; it is cut off before comparing. A handoff the gate
    replaced with its own message (a fail-closed reply) differs from what was sent and so is not
    unchanged.
    """
    pinned = gated and boundary.startswith("delegation:")
    sent: Counter[str] = Counter()
    got: Counter[str] = Counter()
    for e in trace:
        if e.boundary != boundary:
            continue
        if isinstance(e, BoundaryOut):
            sent[e.content.strip()] += 1
        elif isinstance(e, BoundaryIn):
            received = e.content
            if pinned and _PIN_MARK in received:
                received = received[: received.rindex(_PIN_MARK)]
            got[received.strip()] += 1
    return bool(got) and sent == got


def text_noop(folder: Path, record: RunRecord) -> bool:
    """True if the finished run in `folder` ran an operator that left its target handoff as it
    was. A run with no readable trace.jsonl is not judged (False): it stays a sample."""
    if record.arm is Arm.natural or record.operator is Operator.none:
        return False
    try:
        trace = parse_trace_jsonl((folder / "trace.jsonl").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return unchanged_at_target(trace, record.boundary, gated=record.arm is Arm.gated)
