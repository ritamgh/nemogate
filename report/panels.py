"""Evidence panels for the results page: the trace at the violating action, the repair patch and
the coverage of the ledger. Pure local; no model call, no network.

Inputs are files the pipeline already writes: run folders (`run_config.json`, `record.json`,
`violations.json`, `trace.jsonl`, and for a fork `fork.json` + `prefix.jsonl`), the folder
`python -m repair` writes (`repair-<fix>.md` + `repair-<fix>.patch`), and `results.json` with the
ledger. Everything that came from a model or a trace goes through `html.escape`.
"""

import difflib
import html
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from schemas.enums import Arm, Operator
from schemas.ledger import Ledger
from schemas.run import RunRecord, Violation, is_comparable
from schemas.trace import BoundaryIn, BoundaryOut, Event, ToolCall, parse_trace_jsonl

CLIP_TEXT = 8000  # characters of a delegation shown; the page is for reading, not for archiving
CLIP_ARGS = 400
SHOWN_SAME_TOOL_CALLS = 5
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")


def e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def clip(text: str, limit: int) -> str:
    return (
        text if len(text) <= limit else text[:limit] + f" ... ({len(text) - limit} more characters)"
    )


# --- run folders ------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunView:
    run_id: str
    record: RunRecord
    violations: tuple[Violation, ...]
    events: tuple[Event, ...]  # the fork's prefix first, then its own events, steps renumbered
    fork_of: str | None

    def at_step(self, step: int) -> Event | None:
        return next((ev for ev in self.events if ev.step == step), None)


@dataclass(frozen=True)
class Exemplar:
    violating: RunView
    comparison: RunView | None


@dataclass(frozen=True)
class TraceEvidence:
    exemplars: dict[tuple[str, str], Exemplar]
    read: int  # run folders loaded
    skipped: int  # run folders with a config but no usable record or trace


def _read_events(path: Path) -> list[Event]:
    return parse_trace_jsonl(path.read_text(encoding="utf-8"))


def load_run(folder: Path) -> RunView:
    """One run folder. A fork's `violations.json` steps count `prefix.jsonl` first, then its own
    `trace.jsonl` (replay/README.md), so the fork's events are renumbered to match."""
    record = RunRecord.model_validate_json((folder / "record.json").read_text(encoding="utf-8"))
    violations_path = folder / "violations.json"
    violations = (
        tuple(Violation.model_validate(v) for v in json.loads(violations_path.read_text("utf-8")))
        if violations_path.is_file()
        else ()
    )
    prefix = _read_events(folder / "prefix.jsonl") if (folder / "prefix.jsonl").is_file() else []
    own = [
        ev.model_copy(update={"step": ev.step + len(prefix)})
        for ev in _read_events(folder / "trace.jsonl")
    ]
    fork_path = folder / "fork.json"
    fork_of = (
        json.loads(fork_path.read_text("utf-8"))["source_run_id"] if fork_path.is_file() else None
    )
    return RunView(folder.name, record, violations, tuple(prefix + own), fork_of)


def load_runs(paths: list[Path]) -> tuple[list[RunView], int]:
    """Every readable run folder under `paths` (a run folder, or a folder of them), sorted by run
    id, and the number of folders skipped because they could not be read."""
    folders: list[Path] = []
    for path in paths:
        if (path / "run_config.json").is_file():
            folders.append(path)
        else:
            folders += sorted(p for p in path.iterdir() if (p / "run_config.json").is_file())
    runs: dict[str, RunView] = {}
    skipped = 0
    for folder in folders:
        try:
            run = load_run(folder)
        except (OSError, ValueError, KeyError, ValidationError):
            skipped += 1
            continue
        runs.setdefault(run.run_id, run)
    return [runs[k] for k in sorted(runs)], skipped


def violated(run: RunView) -> bool:
    return run.record.constraint_id in run.record.violations


def find_comparison(run: RunView, runs: list[RunView]) -> RunView | None:
    """A run of the same task, rule and boundary that did not break the rule and was made under
    the same conditions: the gated run with the same operator if there is one, else a natural
    run. The first by run id."""
    same = [
        r
        for r in runs
        if r.run_id != run.run_id
        and (r.record.task_id, r.record.constraint_id, r.record.boundary)
        == (run.record.task_id, run.record.constraint_id, run.record.boundary)
        and not violated(r)
        and is_comparable(run.record, r.record)
    ]
    gated = [
        r for r in same if r.record.arm == Arm.gated and r.record.operator == run.record.operator
    ]
    natural = [
        r for r in same if r.record.arm == Arm.natural and r.record.operator == Operator.none
    ]
    return (gated or natural or [None])[0]


def pick_exemplars(runs: list[RunView]) -> dict[tuple[str, str], Exemplar]:
    """Per (rule, boundary): the first violating mutated run (by run id) that has a comparison
    run, else the first violating mutated run."""
    by_key: dict[tuple[str, str], list[RunView]] = {}
    for run in runs:
        if run.record.arm == Arm.mutated and violated(run):
            by_key.setdefault((run.record.constraint_id, run.record.boundary), []).append(run)
    out = {}
    for key, candidates in by_key.items():
        pairs = [Exemplar(r, find_comparison(r, runs)) for r in candidates]
        out[key] = next((p for p in pairs if p.comparison), pairs[0])
    return out


def load_trace_evidence(paths: list[Path]) -> TraceEvidence:
    runs, skipped = load_runs(paths)
    return TraceEvidence(pick_exemplars(runs), len(runs), skipped)


# --- the trace panel --------------------------------------------------------------------------


def handoff(run: RunView, boundary: str) -> tuple[BoundaryOut, BoundaryIn, int] | None:
    """The first send and receive at the boundary, and how many receives there were."""
    sent = [ev for ev in run.events if isinstance(ev, BoundaryOut) and ev.boundary == boundary]
    got = [ev for ev in run.events if isinstance(ev, BoundaryIn) and ev.boundary == boundary]
    return (sent[0], got[0], len(got)) if sent and got else None


def sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_BREAK.split(text.strip()) if s.strip()]


def render_diff(sent: str, received: str) -> str:
    """The planner's text against what the receiver got, one sentence per line: a struck line was
    sent and never arrived, an added line arrived without being sent."""
    a, b = sentences(clip(sent, CLIP_TEXT)), sentences(clip(received, CLIP_TEXT))
    lines = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            lines += [f'<p class="ln">{e(s)}</p>' for s in a[i1:i2]]
            continue
        lines += [f'<p class="ln"><del>{e(s)}</del></p>' for s in a[i1:i2]]
        lines += [f'<p class="ln"><ins>{e(s)}</ins></p>' for s in b[j1:j2]]
    note = (
        '<p class="note">Identical to what the planner sent: nothing was changed in transit.</p>'
        if a == b
        else ""
    )
    return f'<div class="diff">{"".join(lines) or "<p class=ln><em>(empty)</em></p>"}</div>{note}'


def args_text(args: dict[str, Any]) -> str:
    return clip(json.dumps(args, sort_keys=True, ensure_ascii=False), CLIP_ARGS)


def describe(ev: Event) -> str:
    """One event as a line of HTML: a tool call by its tool and arguments, anything else by type."""
    if isinstance(ev, ToolCall):
        blocked = " (blocked by the gate)" if ev.blocked else ""
        return f"<code>{e(ev.tool)}</code> <code>{e(args_text(ev.args))}</code>{e(blocked)}"
    content = getattr(ev, "content", None) or getattr(ev, "reason", "") or ""
    return f"{e(ev.type)}: {e(clip(str(content), CLIP_ARGS))}"


def kv(rows: list[tuple[str, str]]) -> str:
    return (
        '<dl class="kv">'
        + "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in rows)
        + "</dl>"
    )


def violating_action(run: RunView) -> tuple[str, Event | None]:
    cid = run.record.constraint_id
    viol = next((v for v in run.violations if v.constraint_id == cid), None)
    if viol is None:
        return "<p>This run's violations.json has no entry for the rule.</p>", None
    check = f"<code>{e(viol.predicate)}</code>: {e(viol.detail)}"
    if viol.step is None:
        return kv(
            [("check", check), ("action", "no single action: judged on the final files")]
        ), None
    ev = run.at_step(viol.step)
    if ev is None:
        return kv([("check", check), ("action", f"step {e(viol.step)} is not in the trace")]), None
    return kv(
        [
            ("check", check),
            ("step", f"{e(ev.step)}, agent <code>{e(ev.agent)}</code>"),
            ("action", describe(ev)),
        ]
    ), ev


def comparison_action(run: RunView, ev: Event | None) -> str:
    if ev is None:
        return "<p>There is no single action to compare.</p>"
    if isinstance(ev, ToolCall):
        calls = [c for c in run.events if isinstance(c, ToolCall) and c.tool == ev.tool]
        same = next((c for c in calls if c.args == ev.args), None)
        if same is not None:
            outcome = "was blocked by the gate" if same.blocked else "went ahead"
            return kv(
                [
                    ("result", f"Made the same call at step {e(same.step)}: it {outcome}."),
                    ("action", describe(same)),
                ]
            )
        other = "".join(f"<li>{describe(c)}</li>" for c in calls[:SHOWN_SAME_TOOL_CALLS])
        more = (
            f"<li>and {len(calls) - SHOWN_SAME_TOOL_CALLS} more</li>"
            if len(calls) > SHOWN_SAME_TOOL_CALLS
            else ""
        )
        seen = (
            f'<p class="note">Its other calls to <code>{e(ev.tool)}</code>:</p>'
            f"<ul>{other}{more}</ul>"
            if calls
            else f'<p class="note">It never called <code>{e(ev.tool)}</code>.</p>'
        )
        return f"<p><strong>No such call.</strong></p>{seen}"
    later = [c for c in run.events if c.type == ev.type and c.agent == ev.agent]
    if not later:
        return f"<p><strong>No such event</strong> from <code>{e(ev.agent)}</code>.</p>"
    return kv(
        [
            ("result", f"Its last {e(ev.type)} from <code>{e(ev.agent)}</code>"),
            ("action", describe(later[-1])),
        ]
    )


def run_label(run: RunView) -> str:
    fork = f", fork of <code>{e(run.fork_of)}</code>" if run.fork_of else ""
    arm, operator = e(run.record.arm.value), e(run.record.operator.value)
    return f"<code>{e(run.run_id)}</code> ({arm}, operator {operator}{fork})"


def side(run: RunView, heading: str, boundary: str, ev_html: str) -> str:
    pair = handoff(run, boundary)
    if pair is None:
        got = "<p>No handoff at this boundary in the trace.</p>"
    else:
        sent, received, crossings = pair
        which = f" (first of {crossings} crossings)" if crossings > 1 else ""
        got = (
            f'<p class="note">What the receiver got{which}, against what the planner sent:</p>'
            + render_diff(sent.content, received.content)
        )
    return (
        f'<div class="side"><h5>{e(heading)}</h5><p class="note">{run_label(run)}</p>{got}'
        f'<p class="note">The action:</p>{ev_html}</div>'
    )


def render_trace_panel(ex: Exemplar | None, evidence: TraceEvidence) -> str:
    if ex is None:
        return (
            '<p class="note"><span class="notrun">Trace: no violating mutated run for this rule at '
            f"this boundary among the {e(evidence.read)} run folders read "
            f"({e(evidence.skipped)} skipped as unreadable).</span></p>"
        )
    boundary = ex.violating.record.boundary
    action_html, ev = violating_action(ex.violating)
    left = side(ex.violating, "Broke the rule", boundary, action_html)
    if ex.comparison is None:
        right = (
            '<div class="side"><h5>Comparison</h5><p><span class="notrun">No gated or natural run '
            "of the same task that kept the rule and ran under the same conditions was found "
            "among the run folders.</span></p></div>"
        )
    else:
        right = side(ex.comparison, "Kept the rule", boundary, comparison_action(ex.comparison, ev))
    return (
        '<details class="trace" open><summary>Trace at the violating action</summary>'
        '<p class="note">Left: the first violating mutated run, by run id. Right: the first run '
        "of the same task that kept the rule, preferring a gated run with the same operator. "
        "<del>Struck</del> sentences were in the planner's text and never reached the receiver; "
        "<ins>added</ins> sentences reached it without being sent. Trace text is model output, "
        f'shown escaped.</p><div class="pair">{left}{right}</div></details>'
    )


# --- the patch panel --------------------------------------------------------------------------

HEADER_RE = re.compile(r"^# Repair: (Accepted|Rejected)\s*$")
FIX_RE = re.compile(r"^- Fix: `([^`]+)` on `([^`]+)`\s*$")
JUDGED_RE = re.compile(
    r"violation rate (\d+%) -> (\d+%), task success (\d+%) -> (\d+%) "
    r"\((\d+) baseline runs, (\d+) patched runs\)"
)


@dataclass(frozen=True)
class RepairView:
    name: str
    accepted: bool
    fix_id: str | None
    agent: str | None
    why: str | None
    result: str
    diff: str | None  # None: the .patch file named by the summary is missing


def field_of(lines: list[str], key: str) -> str | None:
    return next((x[len(key) :] for x in lines if x.startswith(key)), None)


def load_repairs(directory: Path) -> list[RepairView]:
    """Each `repair-*.md` summary `repair.loop.write_report` wrote, with the diff beside it."""
    if not directory.is_dir():
        raise FileNotFoundError(f"repair folder {directory} does not exist")
    repairs = []
    for summary in sorted(directory.glob("repair-*.md")):
        lines = summary.read_text(encoding="utf-8").splitlines()
        head = HEADER_RE.match(lines[0]) if lines else None
        if head is None:
            raise ValueError(f"{summary}: not a repair summary (no '# Repair: Accepted|Rejected')")
        fix = next((m for m in map(FIX_RE.match, lines) if m), None)
        result = field_of(lines, "- Result: ")
        if result is None:
            raise ValueError(f"{summary}: no '- Result:' line")
        patch = summary.with_suffix(".patch")
        repairs.append(
            RepairView(
                name=summary.stem,
                accepted=head.group(1) == "Accepted",
                fix_id=fix.group(1) if fix else None,
                agent=fix.group(2) if fix else None,
                why=field_of(lines, "- Why: "),
                result=result,
                diff=patch.read_text(encoding="utf-8") if patch.is_file() else None,
            )
        )
    return repairs


def render_patch_lines(diff: str) -> str:
    out = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            cls = "file"
        elif line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        out.append(f'<span class="{cls}">{e(line)}</span>')
    return '<pre class="patch" tabindex="0">' + "".join(out) + "</pre>"


def render_repair(r: RepairView) -> str:
    verdict = "Accepted" if r.accepted else "Rejected"
    fix = (
        f"<code>{e(r.fix_id)}</code> on the <code>{e(r.agent)}</code> agent"
        if r.fix_id
        else "no fix was chosen"
    )
    judged = JUDGED_RE.search(r.result)
    numbers = ""
    if judged:
        v0, v1, s0, s1, n0, n1 = judged.groups()
        numbers = (
            '<table class="plain"><thead><tr><th scope="col">Judged on</th>'
            '<th scope="col">Before</th><th scope="col">After the patch</th></tr></thead><tbody>'
            + "".join(
                f'<tr><th scope="row">{label}</th><td class="num">{e(before)}</td>'
                f'<td class="num">{e(after)}</td></tr>'
                for label, before, after in (
                    ("Violation rate", v0, v1),
                    ("Task success", s0, s1),
                    ("Runs", n0, n1),
                )
            )
            + "</tbody></table>"
        )
    diff = (
        render_patch_lines(r.diff)
        if r.diff
        else '<p class="note"><span class="notrun">'
        + (
            "The patch file is missing."
            if r.diff is None
            else "No diff: the patch was refused before it was applied."
        )
        + "</span></p>"
    )
    why = f'<p class="note">Why: {e(r.why)}</p>' if r.why else ""
    return (
        f'<article class="constraint repair"><header><h3>{fix}</h3>'
        f'<span class="badge {"yes" if r.accepted else "no"}">{verdict}</span></header>'
        f'{why}<p class="note">Result: {e(r.result)}</p>{numbers}{diff}</article>'
    )


def render_patch_panel(repairs: list[RepairView] | None) -> str:
    head = '<section aria-labelledby="patch-h"><h2 id="patch-h">The repair patch</h2>'
    if repairs is None:
        return (
            f'{head}<div class="empty"><strong>No repair run yet.</strong> Pass '
            "<code>--repair DIR</code>, the folder <code>python -m repair</code> wrote, to show "
            "the proposed patch and the verdict.</div></section>"
        )
    if not repairs:
        return (
            f'{head}<div class="empty"><strong>The repair folder has no repair summary.</strong> '
            "Expected <code>repair-*.md</code> and <code>repair-*.patch</code>.</div></section>"
        )
    return head + "".join(render_repair(r) for r in repairs) + "</section>"


# --- the coverage panel -----------------------------------------------------------------------


def exercised(constraints: dict[str, Any]) -> list[str]:
    """Constraint ids with at least one pooled run."""
    return sorted(
        cid
        for cid, body in constraints.items()
        if any(c["n"] > 0 for b in body.get("boundaries", {}).values() for c in b.get("cells", []))
    )


def rule_list(items: list[tuple[str, str]]) -> str:
    if not items:
        return '<p class="note">None.</p>'
    return (
        '<ul class="rules">'
        + "".join(f"<li><code>{e(i)}</code> {e(t)}</li>" for i, t in items)
        + "</ul>"
    )


def render_coverage(results: dict[str, Any], ledger: Ledger | None) -> str:
    honesty, constraints = results["honesty"], results["constraints"]
    total, checked = honesty.get("constraints_total", 0), honesty.get("constraints_with_check", 0)
    run_ids = exercised(constraints)
    run_and_checked = [c for c in run_ids if constraints[c].get("has_check")]
    recall = honesty.get("extraction_recall")
    recall_text = (
        "not measured" if recall is None else f"{int(math.floor(recall * 100 + 0.5 + 1e-9))}%"
    )
    rows = [
        ("Declared in the ledger", f"{e(total)}"),
        ("With a compiled check", f"{e(checked)} of {e(total)}"),
        ("Exercised: at least one pooled run", f"{e(len(run_ids))} of {e(total)}"),
        (
            "Exercised and checked: the only ones with a verdict",
            f"{e(len(run_and_checked))} of {e(total)}",
        ),
        ("Extraction recall", e(recall_text)),
    ]
    table = (
        '<div class="tablewrap"><table class="plain"><tbody>'
        + "".join(f'<tr><th scope="row">{k}</th><td class="num">{v}</td></tr>' for k, v in rows)
        + "</tbody></table></div>"
    )
    if ledger is not None:
        unchecked = [(c.id, c.text) for c in ledger.constraints if c.check is None]
        unexercised = [(c.id, c.text) for c in ledger.constraints if c.id not in run_ids]
        mismatch = (
            f'<p class="warn">The ledger lists {e(len(ledger.constraints))} constraints but '
            f"results.json counts {e(total)}: they may not be the same ledger.</p>"
            if len(ledger.constraints) != total
            else ""
        )
        never = f"<h3>Never exercised (no pooled run)</h3>{rule_list(unexercised)}"
    else:
        unchecked = [
            (c, constraints[c].get("text", ""))
            for c in sorted(constraints)
            if not constraints[c].get("has_check")
        ]
        mismatch = ""
        never = (
            f'<p class="note">{e(max(total - len(run_ids), 0))} declared constraints were never '
            "exercised, and constraints that never ran are missing from the list above. Pass "
            "<code>--ledger</code> to list them.</p>"
        )
    return (
        '<section aria-labelledby="coverage-h"><h2 id="coverage-h">Coverage</h2>'
        '<p class="note">How much of the ledger these numbers speak for. A constraint without a '
        "compiled check cannot be graded; one that never came up in a run has no rate.</p>"
        f"{table}{mismatch}<h3>Without a compiled check (cannot be graded)</h3>"
        f"{rule_list(unchecked)}{never}</section>"
    )
