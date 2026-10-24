"""One rule's life as a static page of steps, built from saved run folders (no model call).

`python -m report.replay_view --out OUT.html --story TITLE --rule-run RUN_DIR --gated-run RUN_DIR
[--natural-run RUN_DIR] [--user-message-file FILE]` writes one self-contained HTML page: the
user's rule, the handoff that lost it, the action that broke it, and the gated run beside it.
Stepping is radio inputs and labels (no script), every text on it is read from the run folders
and escaped, and the oracle's verdict is the saved `violations.json`, never recomputed. The rule
and its text anchor come from each run's own `ledger.yaml` (`replay.carriage.anchors_of`).
A run folder that lacks a file a step needs is exit code 2 naming it.
"""

import argparse
import difflib
import html
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gate.middleware import REPIN
from replay.carriage import anchors_of
from report import panels
from schemas.ledger import load_ledger_yaml
from schemas.run import Violation
from schemas.trace import (
    BoundaryIn,
    BoundaryOut,
    Event,
    GateBlock,
    ToolCall,
    ToolResult,
    parse_trace_jsonl,
)

CSS = (Path(__file__).parent / "site.css").read_text(encoding="utf-8") + (
    Path(__file__).parent / "replay.css"
).read_text(encoding="utf-8")
REQUIRED = ("run_config.json", "record.json", "trace.jsonl", "violations.json", "ledger.yaml")
PIN_MARK = REPIN.partition("{rules}")[0].strip()  # what the gate puts before the rules it pins

HANDOFF_CHARS = 2500  # characters of a handoff or message shown
ARGS_CHARS = 400
RESULT_CHARS = 300
REPLY_CHARS = 500
SHOWN_BLOCKS = 3  # blocked calls listed
SHOWN_NEXT = 3  # calls listed after the first block
LIST_ARGS_CHARS = 200  # arguments shown for a call in a list

ARM_WORDS = {
    "natural": "no sabotage and no gate",
    "mutated": "an operator edits the handoff on its way",
    "block_only": "the gate blocks forbidden calls and says nothing more",
    "gated": "the gate blocks forbidden calls and re-pins the rule in the handoff",
}
OPERATOR_WORDS = {
    "none": "no edit",
    "drop": "removes the sentence that carries the rule",
    "hedge": "turns the rule into vague advice",
    "compress_100": "summarises the handoff to 100 words",
    "compress_25": "summarises the handoff to 25 words",
    "return_corrupt": "corrupts the reply on the return boundary",
}


class ViewError(ValueError):
    """A run folder cannot tell this story; the message names the file."""


def e(value: Any) -> str:
    return html.escape(str(value), quote=True)


# --- reading the run folders -----------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    folder: Path
    run_id: str
    task_id: str
    arm: str
    operator: str
    constraint_id: str
    boundary: str
    task_success: bool | None
    rule_text: str
    rule_kind: str
    check: str
    anchors: tuple[str, ...]
    events: tuple[Event, ...]  # a fork's prefix first, then its own events, steps renumbered
    prefix_len: int
    fork_of: str | None
    violations: tuple[Violation, ...]  # the target rule's entries only

    @property
    def short(self) -> str:
        return self.run_id.rsplit("-", 1)[-1]

    @property
    def broke(self) -> bool:
        return bool(self.violations)

    @property
    def source(self) -> str:
        return self.fork_of or self.run_id


def _text(folder: Path, name: str) -> str:
    path = folder / name
    if not path.is_file():
        raise ViewError(f"{path}: missing")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ViewError(f"{path}: cannot be read ({exc})") from exc


def _json(folder: Path, name: str) -> Any:
    try:
        return json.loads(_text(folder, name))
    except json.JSONDecodeError as exc:
        raise ViewError(f"{folder / name}: not valid JSON ({exc})") from exc


def _events(folder: Path, name: str) -> list[Event]:
    try:
        return parse_trace_jsonl(_text(folder, name))
    except ViewError:
        raise
    except ValueError as exc:
        raise ViewError(f"{folder / name}: {str(exc).splitlines()[0]}") from exc


def _key(data: Any, key: str, path: Path) -> Any:
    if not isinstance(data, dict) or key not in data:
        raise ViewError(f"{path}: has no {key!r}")
    return data[key]


def load_run(folder: Path) -> Run:
    config = _json(folder, "run_config.json")
    cfg_path = folder / "run_config.json"
    constraint_id = _key(config, "target_constraint", cfg_path)
    record = _json(folder, "record.json")
    try:
        ledger = load_ledger_yaml(_text(folder, "ledger.yaml"))
        rule = ledger.get(constraint_id)
    except KeyError as exc:
        raise ViewError(f"{folder / 'ledger.yaml'}: has no rule {constraint_id}") from exc
    except ValueError as exc:
        raise ViewError(f"{folder / 'ledger.yaml'}: {str(exc).splitlines()[0]}") from exc
    try:
        violations = tuple(
            v
            for v in (Violation.model_validate(x) for x in _json(folder, "violations.json"))
            if v.constraint_id == constraint_id
        )
    except (ValueError, TypeError) as exc:
        raise ViewError(f"{folder / 'violations.json'}: {str(exc).splitlines()[0]}") from exc
    own = _events(folder, "trace.jsonl")
    prefix: list[Event] = []
    fork_of = None
    if (folder / "prefix.jsonl").is_file():
        prefix = _events(folder, "prefix.jsonl")
        fork_of = _key(_json(folder, "fork.json"), "source_run_id", folder / "fork.json")
    n = len(prefix)
    events = (*prefix, *(ev.model_copy(update={"step": ev.step + n}) for ev in own))
    check = rule.check
    dump = check.model_dump(mode="json") if check is not None else {}
    detail = ", ".join(f"{k}: {v}" for k, v in dump.items() if k != "type")
    return Run(
        folder=folder,
        run_id=_key(config, "run_id", cfg_path),
        task_id=_key(config, "task_id", cfg_path),
        arm=_key(config, "arm", cfg_path),
        operator=_key(config, "operator", cfg_path),
        constraint_id=constraint_id,
        boundary=_key(config, "target_boundary", cfg_path),
        task_success=record.get("task_success") if isinstance(record, dict) else None,
        rule_text=rule.text,
        rule_kind=rule.kind,
        check=f"{dump['type']} ({detail})" if dump else "no check",
        anchors=anchors_of(check),
        events=events,
        prefix_len=n,
        fork_of=fork_of,
        violations=violations,
    )


def handoff(run: Run) -> tuple[BoundaryOut, BoundaryIn]:
    """The first send and receive at the run's own boundary."""
    sent = next(
        (x for x in run.events if isinstance(x, BoundaryOut) and x.boundary == run.boundary), None
    )
    got = next(
        (x for x in run.events if isinstance(x, BoundaryIn) and x.boundary == run.boundary), None
    )
    if sent is None or got is None:
        raise ViewError(
            f"{run.folder / 'trace.jsonl'}: no handoff at {run.boundary} (a send and a receive)"
        )
    return sent, got


def agents_of(boundary: str) -> tuple[str, str]:
    match = re.fullmatch(r"[a-z]+:(.+)->(.+)", boundary)
    return (match.group(1), match.group(2)) if match else ("sender", "receiver")


# --- small renderers -------------------------------------------------------------------------


def shown(text: str, limit: int) -> str:
    """Escaped text cut at `limit` characters; a cut says so where it is, with an ellipsis."""
    if len(text) <= limit:
        return e(text)
    more = len(text) - limit
    return f'{e(text[:limit])}… <span class="tag">({more} more characters not shown)</span>'


def marked(text: str, anchors: Sequence[str]) -> str:
    """Escaped text with every occurrence of an anchor in <mark>."""
    if not anchors:
        return e(text)
    pattern = re.compile(
        "|".join(re.escape(a) for a in sorted(anchors, key=len, reverse=True)), re.I
    )
    out, pos = [], 0
    for hit in pattern.finditer(text):
        out += [e(text[pos : hit.start()]), f"<mark>{e(hit.group(0))}</mark>"]
        pos = hit.end()
    return "".join(out) + e(text[pos:])


def count(text: str, anchors: Sequence[str]) -> int:
    if not anchors:
        return 0
    return len(re.findall("|".join(re.escape(a) for a in anchors), text, re.I))


def times(n: int) -> str:
    return f"{n} time" + ("" if n == 1 else "s")


def kv(rows: list[tuple[str, str]]) -> str:
    return panels.kv(rows)


def split_pin(received: str) -> tuple[str, str]:
    """The received text without the gate's pinned-rules block, and that block ('' if none)."""
    at = received.find(PIN_MARK)
    return (received, "") if at < 0 else (received[:at], received[at:])


def lines(text: str, anchors: Sequence[str]) -> str:
    cut = text[:HANDOFF_CHARS]
    body = "".join(
        f'<p class="ln"><span class="t">{marked(p, anchors)}</span></p>'
        for p in panels.sentences(cut)
    )
    return f'<div class="diff">{body or "<p class=ln><em>(empty)</em></p>"}</div>' + (
        f'<p class="note">… {len(text) - HANDOFF_CHARS} more characters not shown.</p>'
        if len(text) > HANDOFF_CHARS
        else ""
    )


def received_lines(sent: str, received: str, anchors: Sequence[str]) -> str:
    """What the receiver got against what was sent: a removed sentence struck in place, an added
    one marked, and the gate's pinned block apart from the planner's own sentences."""
    body, pin = split_pin(received)
    a, b = panels.sentences(sent[:HANDOFF_CHARS]), panels.sentences(body[:HANDOFF_CHARS])
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            out += [
                f'<p class="ln"><span class="t">{marked(s, anchors)}</span></p>' for s in a[i1:i2]
            ]
            continue
        out += [
            f'<p class="ln"><del>{marked(s, anchors)}</del> <span class="tag">(removed)</span></p>'
            for s in a[i1:i2]
        ]
        out += [
            f'<p class="ln"><ins>{marked(s, anchors)}</ins> <span class="tag">(added)</span></p>'
            for s in b[j1:j2]
        ]
    if pin:
        out.append('<p class="ln pinhead"><span class="tag">Added by the gate:</span></p>')
        out += [
            f'<p class="ln"><ins>{marked(s, anchors)}</ins> '
            '<span class="tag">(re-pinned by the gate)</span></p>'
            for s in panels.sentences(pin[:HANDOFF_CHARS])
        ]
    cut = max(len(sent), len(body), len(pin)) > HANDOFF_CHARS
    note = (
        '<p class="note">Long text: only the first characters are compared and shown.</p>'
        if cut
        else ""
    )
    return f'<div class="diff">{"".join(out) or "<p class=ln><em>(empty)</em></p>"}</div>{note}'


def call_text(ev: Event, limit: int = ARGS_CHARS) -> str:
    if isinstance(ev, ToolCall):
        args = json.dumps(ev.args, sort_keys=True, ensure_ascii=False)
        flag = " (blocked by the gate)" if ev.blocked else ""
        return f"<code>{e(ev.tool)}</code> <code>{shown(args, limit)}</code>{e(flag)}"
    content = getattr(ev, "content", None) or getattr(ev, "reason", "") or ""
    return f"{e(ev.type)}: {shown(str(content), ARGS_CHARS)}"


def label(run: Run) -> str:
    return f"Run {e(run.short)} ({e(run.arm)}, operator {e(run.operator)})"


def step_note(run: Run) -> str:
    return (
        f" (a fork: steps count its {run.prefix_len} earlier events first)"
        if run.prefix_len
        else ""
    )


def at_step(run: Run, step: int) -> Event | None:
    return next((x for x in run.events if x.step == step), None)


def result_after(run: Run, ev: Event) -> str:
    nxt = at_step(run, ev.step + 1)
    if isinstance(ev, ToolCall) and isinstance(nxt, ToolResult) and nxt.tool == ev.tool:
        return shown(nxt.content, RESULT_CHARS)
    return ""


def verdict_rows(run: Run) -> list[tuple[str, str]]:
    """The saved verdict (violations.json) for the rule, as rows; nothing is recomputed."""
    if not run.violations:
        return [("Saved verdict", "kept the rule: violations.json has no entry for it")]
    v = next((x for x in run.violations if x.step is not None), run.violations[0])
    return [
        ("Saved verdict", "broke the rule (violations.json)"),
        ("Check", f"<code>{e(v.predicate)}</code>: {e(v.detail)}"),
    ]


def task_line(run: Run) -> str:
    done = {
        True: "passed the task check",
        False: "did not pass the task check",
        None: "has no task result recorded",
    }
    return f"Run {e(run.short)} {done[run.task_success]} (record.json)."


# --- the steps -------------------------------------------------------------------------------


def step_rule(rule_run: Run, user_message: str | None) -> tuple[str, str]:
    anchors = rule_run.anchors
    if user_message is None:
        said = (
            '<p class="note">The user\'s message is not in the run folder, and no task file was '
            "given, so only the rule as the run's ledger.yaml states it is shown.</p>"
        )
    else:
        said = (
            f'<blockquote class="said">{marked(user_message[:HANDOFF_CHARS], anchors)}</blockquote>'
            '<p class="note">The user\'s message, from the task file (the run folder does not '
            "hold it)"
            + (
                f"; {len(user_message) - HANDOFF_CHARS} more characters not shown"
                if len(user_message) > HANDOFF_CHARS
                else ""
            )
            + ".</p>"
        )
    anchor = (
        ", ".join(f"<mark>{e(a)}</mark>" for a in anchors)
        if anchors
        else "none: this rule's check has no text anchor, so nothing is highlighted on this page"
    )
    rows = kv(
        [
            ("The rule, as the ledger states it", e(rule_run.rule_text)),
            ("Kind", e(rule_run.rule_kind)),
            ("Check", f"<code>{e(rule_run.check)}</code>"),
            ("Must survive the handoff at", f"<code>{e(rule_run.boundary)}</code>"),
            ("Text the page highlights", anchor),
        ]
    )
    return "The user's rule", said + rows


def step_handoff(rule_run: Run, natural: Run | None) -> tuple[str, str]:
    sender, receiver = agents_of(rule_run.boundary)
    sent, got = handoff(rule_run)
    body, _ = split_pin(got.content)
    anchors = rule_run.anchors
    head = (
        f'<p class="lede">The {e(sender)} handed the work to the {e(receiver)}: the first handoff '
        f"at <code>{e(rule_run.boundary)}</code> in run {e(rule_run.short)} "
        f"({e(rule_run.arm)}, operator {e(rule_run.operator)}).</p>"
    )
    sent_block = f"<h3>What the {e(sender)} sent</h3>" + lines(sent.content, anchors)
    if panels.sentences(sent.content) == panels.sentences(body):
        verdict = (
            f'<p class="note">Nothing was removed from the {e(sender)}\'s text in transit: its '
            "sentences arrived as sent.</p>"
        )
    else:
        verdict = ""
    counted = ""
    if anchors:
        counted = (
            f'<p class="count-line">The text <code>{e(", ".join(anchors))}</code> appears '
            f"{times(count(sent.content, anchors))} in what the {e(sender)} sent and "
            f"{times(count(body, anchors))} in what the {e(receiver)} received.</p>"
        )
    got_block = (
        f"<h3>What the {e(receiver)} received</h3>"
        + received_lines(sent.content, got.content, anchors)
        + verdict
        + counted
    )
    extra = ""
    if natural is not None:
        n_sent, n_got = handoff(natural)
        extra = (
            "<h3>For contrast, a run with no sabotage</h3>"
            f'<p class="note">Run {e(natural.short)} ({e(natural.arm)}, operator '
            f"{e(natural.operator)}), the same task: its first handoff at the same boundary, as "
            "saved.</p>" + received_lines(n_sent.content, n_got.content, natural.anchors)
        )
    return "The handoff, and what happened to the rule", head + sent_block + got_block + extra


def step_action(rule_run: Run) -> tuple[str, str]:
    viol = next((v for v in rule_run.violations if v.step is not None), rule_run.violations[0])
    check = f"<code>{e(viol.predicate)}</code>: {e(viol.detail)}"
    rows = [("Saved verdict", "broke the rule (violations.json)"), ("Check", check)]
    ev = at_step(rule_run, viol.step) if viol.step is not None else None
    if viol.step is None:
        rows.append(
            (
                "Action",
                "no single action: judged on the final files"
                if viol.predicate != "output_must_include"
                else "no reply to grade: the agent's reply never arrived",
            )
        )
    elif ev is None:
        rows.append(("Action", f"step {e(viol.step)} is not in the trace"))
    else:
        rows += [
            (
                "Where",
                f"step {e(ev.step)}, agent <code>{e(ev.agent)}</code>{e(step_note(rule_run))}",
            ),
            ("Call", call_text(ev)),
        ]
        after = result_after(rule_run, ev)
        if after:
            rows.append(("Result", after))
    _, receiver = agents_of(rule_run.boundary)
    lede = (
        f'<p class="lede">What the {e(receiver)} did next in run {e(rule_run.short)} '
        f"({e(rule_run.arm)}, operator {e(rule_run.operator)}): the saved verdict and the action "
        "it names.</p>"
    )
    return "The action that broke the rule", lede + kv(
        rows
    ) + f'<p class="note">{task_line(rule_run)}</p>'


def blocks_of(run: Run) -> list[tuple[ToolCall, GateBlock | None]]:
    out = []
    for ev in run.events:
        if isinstance(ev, ToolCall) and ev.blocked:
            reason = next(
                (
                    x
                    for x in run.events
                    if isinstance(x, GateBlock) and x.step > ev.step and x.tool == ev.tool
                ),
                None,
            )
            out.append((ev, reason))
    return out


def gated_side(gated: Run) -> str:
    _, receiver = agents_of(gated.boundary)
    sent, got = handoff(gated)
    parts = [
        f'<p class="note">{label(gated)}.</p>',
        f"<h4>What the {e(receiver)} received</h4>",
        received_lines(sent.content, got.content, gated.anchors),
    ]
    blocked = blocks_of(gated)
    if blocked:
        items = "".join(
            f"<li>{call_text(call)}"
            + (
                f"<br><span class='note'>Gate: {shown(why.reason, ARGS_CHARS)}</span>"
                if why
                else ""
            )
            + "</li>"
            for call, why in blocked[:SHOWN_BLOCKS]
        )
        more = (
            f"<li>and {len(blocked) - SHOWN_BLOCKS} more blocked calls</li>"
            if len(blocked) > SHOWN_BLOCKS
            else ""
        )
        parts += [f"<h4>The calls the gate refused ({len(blocked)})</h4><ul>{items}{more}</ul>"]
        first = blocked[0][0]
        nxt = [
            x
            for x in gated.events
            if isinstance(x, ToolCall) and x.step > first.step and not x.blocked
        ][:SHOWN_NEXT]
        if nxt:
            parts.append(
                f"<h4>The {e(receiver)}'s next calls after the first refusal</h4><ul>"
                + "".join(f"<li>{call_text(x, LIST_ARGS_CHARS)}</li>" for x in nxt)
                + "</ul>"
            )
    else:
        parts.append(
            f"<h4>The forbidden call</h4><p>No call was blocked in this run: the {e(receiver)} "
            "never made one.</p>"
        )
    reply = next(
        (
            x
            for x in gated.events
            if isinstance(x, BoundaryOut)
            and x.agent == receiver
            and x.boundary.startswith("return:")
        ),
        None,
    )
    if reply is not None:
        parts += [
            f"<h4>What the {e(receiver)} replied</h4>"
            f'<p class="quote">{shown(reply.content, REPLY_CHARS)}</p>'
        ]
    parts += [kv(verdict_rows(gated)), f'<p class="note">{task_line(gated)}</p>']
    return "".join(parts)


def rule_side(rule_run: Run, has_action_step: bool) -> str:
    _, receiver = agents_of(rule_run.boundary)
    _, got = handoff(rule_run)
    body, _ = split_pin(got.content)
    parts = [f'<p class="note">{label(rule_run)}.</p>']
    if rule_run.anchors:
        parts.append(
            f"<p>The text <code>{e(', '.join(rule_run.anchors))}</code> appears "
            f"{times(count(body, rule_run.anchors))} in what the {e(receiver)} received.</p>"
        )
    parts.append(kv(verdict_rows(rule_run)))
    if has_action_step:
        viol = next((v for v in rule_run.violations if v.step is not None), None)
        ev = at_step(rule_run, viol.step) if viol is not None else None
        if ev is not None:
            parts.append(kv([("The call", call_text(ev))]))
    parts.append(f'<p class="note">{task_line(rule_run)}</p>')
    return "".join(parts)


def step_gated(rule_run: Run, gated: Run, has_action_step: bool) -> tuple[str, str]:
    lede = (
        '<p class="lede">The same rule and the same boundary, with the gate on. Left: the '
        "sabotaged run. Right: the gated run.</p>"
    )
    pair = (
        '<div class="pair">'
        f'<div class="side"><h3>Without the gate</h3>{rule_side(rule_run, has_action_step)}</div>'
        f'<div class="side"><h3>With the gate</h3>{gated_side(gated)}</div></div>'
    )
    return "The gated run beside it", lede + pair


# --- the page --------------------------------------------------------------------------------


def runs_table(roles: list[tuple[str, Run]]) -> str:
    rows = "".join(
        f"<tr><th scope='row'>{e(role)}</th><td><code>{e(run.short)}</code></td>"
        f"<td>{e(run.arm)}</td><td>{e(run.operator)}</td><td>{e(run.task_id)}</td>"
        f"<td><code>{e(run.run_id)}</code></td></tr>"
        for role, run in roles
    )
    head = "".join(
        f"<th scope='col'>{h}</th>" for h in ("Role", "Run", "Arm", "Operator", "Task", "Full id")
    )
    table = (
        "<table class='runs'><caption>Runs on this page</caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
    )
    return panels.tablewrap("Runs on this page", table)


def relation(rule_run: Run, gated: Run) -> str:
    if rule_run.source == gated.source:
        return (
            "The two runs are forks of the same saved run, so they share what the planner did "
            "before the fork point."
        )
    return (
        "The two runs are separate runs of the same task and rule, not forks of one source: "
        "their planner handoffs are different texts, so this is not a matched pair."
    )


def words(runs: list[Run]) -> str:
    arms = sorted({r.arm for r in runs})
    ops = sorted({r.operator for r in runs})
    return (
        "<ul class='words'>"
        + "".join(f"<li><b>{e(a)}</b>: {e(ARM_WORDS.get(a, ''))}</li>" for a in arms)
        + "".join(f"<li>operator <b>{e(o)}</b>: {e(OPERATOR_WORDS.get(o, ''))}</li>" for o in ops)
        + "</ul>"
    )


def build(
    story: str,
    *,
    rule_run: Path,
    gated_run: Path,
    natural_run: Path | None = None,
    user_message: str | None = None,
) -> str:
    rule, gated = load_run(rule_run), load_run(gated_run)
    natural = load_run(natural_run) if natural_run else None
    for other in (gated, natural):
        if other is not None and (other.task_id, other.constraint_id, other.boundary) != (
            rule.task_id,
            rule.constraint_id,
            rule.boundary,
        ):
            raise ViewError(
                f"{other.folder}: not about the same rule as {rule.folder} "
                f"(task {other.task_id}, rule {other.constraint_id}, boundary {other.boundary} "
                f"against task {rule.task_id}, rule {rule.constraint_id}, boundary {rule.boundary})"
            )
    for run in (rule, gated, *([natural] if natural else [])):
        handoff(run)
    steps = [step_rule(rule, user_message), step_handoff(rule, natural)]
    left_out = ""
    if rule.violations:
        steps.append(step_action(rule))
    else:
        left_out = (
            f'<p class="note">The step that shows the action which broke the rule is left out: run '
            f"{e(rule.short)}'s violations.json has no entry for the rule, so it kept the rule.</p>"
        )
    steps.append(step_gated(rule, gated, bool(rule.violations)))
    total = len(steps)
    radios = "".join(
        f'<input type="radio" name="step" id="s{i}"{" checked" if i == 1 else ""}>'
        f'<label class="tab" for="s{i}"><span class="n">{i}</span> {e(title)}</label>'
        for i, (title, _) in enumerate(steps, 1)
    )
    panes = "".join(
        f'<section class="panel" id="p{i}"><h2>Step {i} of {total}: {e(title)}</h2>{body}</section>'
        for i, (title, body) in enumerate(steps, 1)
    )
    roles = [("Sabotaged run", rule), ("Gated run", gated)] + (
        [("No-sabotage run", natural)] if natural else []
    )
    everyone = [r for _, r in roles]
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{e(story)}</title><style>{CSS}</style></head><body><main class="story">'
        f"<header><h1>{e(story)}</h1>"
        '<p class="lead">One rule, followed from the user\'s message to what each agent did, '
        "from saved run folders. Pick a step.</p></header>"
        '<div class="howto"><h2>How to read this</h2><ol>'
        "<li>Use the numbered steps. With a keyboard, tab to them and use the arrow keys. "
        "Printing shows every step.</li>"
        "<li>Every line and number comes from the saved files of the runs listed at the foot; "
        "no model was called to make this page.</li></ol>"
        f"{words(everyone)}</div>"
        f"{left_out}"
        f'<div class="steps">{radios}<div class="panels">{panes}</div></div>'
        "<footer>"
        f"{runs_table(roles)}"
        f'<p class="note">{e(relation(rule, gated))}</p>'
        '<p class="note">This page tells the story of one run, set beside a gated run of the '
        "same task: a story, not a statistic. It shows what happened once, and says nothing about "
        "how often. It links nothing outside itself.</p>"
        "</footer></main></body></html>"
    )


def registry_message(task_id: str) -> str | None:
    """The user message of a task, from the default app's task registry."""
    from replay.carriage import load_user_messages

    return load_user_messages().get(task_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One rule's life as a static page of steps.")
    parser.add_argument("--out", type=Path, required=True, help="where to write the page")
    parser.add_argument("--story", required=True, help="the page title")
    parser.add_argument(
        "--rule-run",
        type=Path,
        required=True,
        metavar="RUN_DIR",
        help="the run where the rule was lost and broken",
    )
    parser.add_argument(
        "--gated-run",
        type=Path,
        required=True,
        metavar="RUN_DIR",
        help="a gated run of the same task and rule",
    )
    parser.add_argument(
        "--natural-run",
        type=Path,
        metavar="RUN_DIR",
        help="optional: a run with no sabotage, for contrast",
    )
    parser.add_argument(
        "--user-message-file",
        type=Path,
        metavar="FILE",
        help="the user's message as text; without it the task registry is used",
    )
    args = parser.parse_args(argv)
    try:
        if args.user_message_file:
            message = args.user_message_file.read_text(encoding="utf-8")
        else:
            try:
                message = registry_message(load_run(args.rule_run).task_id)
            except (ImportError, KeyError):
                message = None
        page = build(
            args.story,
            rule_run=args.rule_run,
            gated_run=args.gated_run,
            natural_run=args.natural_run,
            user_message=message,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(page, encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"report.replay_view: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
