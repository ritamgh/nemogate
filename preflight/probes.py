"""The C0 probes. Each takes a model from `get_model(role)` and returns a `ProbeResult`.

A probe makes `tries` calls and counts how many met its pass criterion; it passes only when all
did. A call that raises counts as an error (and fails the try), except `SpendCapReached`, which
always propagates: the cap is not a probe finding.
"""

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from common.spend import SpendCapReached
from schemas.enums import Role

ResponseFormat = Literal["json_schema", "json_object"]
RESPONSE_FORMATS: tuple[ResponseFormat, ...] = ("json_schema", "json_object")

# Quotes, backslashes (including ones that look like escapes) and a newline.
ESCAPE_SAMPLE = 'He said "hi" at C:\\temp\\new\nsecond line'
TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}
EXTRACTION_TEXT = "Dana flies from Oslo to Lima on 14 Nov."
EXTRACTION_FIELDS = {"person": "Dana", "origin": "Oslo", "destination": "Lima"}

_TEXT_MARKERS = ("<tool_call>", "<function=", "[TOOL_CALLS]", "<|python_tag|>")
_ARGUMENT_KEYS = ("arguments", "parameters", "args")
_EVIDENCE_LIMIT = 3  # failure notes kept per result


@dataclass(frozen=True)
class Variant:
    """The settings one model is built with: the safe-config values a probe tries."""

    role: Role
    response_format: ResponseFormat
    thinking: bool


@dataclass(frozen=True)
class ProbeResult:
    probe: str
    role: Role
    response_format: ResponseFormat | None
    thinking: bool | None
    tries: int
    ok: int  # tries that met the pass criterion
    counts: dict[str, int] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()  # short notes on the first failures

    @property
    def passed(self) -> bool:
        return self.tries > 0 and self.ok == self.tries


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        block if isinstance(block, str) else str(block.get("text", "")) for block in content
    )


def _clip(text: str, limit: int = 80) -> str:
    return repr(text if len(text) <= limit else text[:limit] + "...")


def _is_call(value: Any, tool_names: Collection[str]) -> bool:
    if isinstance(value, list):
        return any(_is_call(item, tool_names) for item in value)
    if not isinstance(value, dict):
        return False
    if "tool_calls" in value and _is_call(value["tool_calls"], tool_names):
        return True
    if "function" in value and _is_call(value["function"], tool_names):
        return True
    name = value.get("name")
    has_arguments = any(key in value for key in _ARGUMENT_KEYS)
    return isinstance(name, str) and has_arguments and (not tool_names or name in tool_names)


def looks_like_tool_call_text(content: str, tool_names: Collection[str] = ()) -> bool:
    """True when `content` holds a tool call written out as text (the NAT #2274 failure class).

    Flags a known call marker (`<tool_call>`, `<function=`, ...) or a JSON object, anywhere in the
    text, that names a tool and carries its arguments. With `tool_names`, only those names count,
    so ordinary JSON data with a `name` key is not a call. Prose that mentions a tool is not one.
    """
    if any(marker in content for marker in _TEXT_MARKERS):
        return True
    decoder = json.JSONDecoder()
    for start, char in enumerate(content):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(content, start)
        except ValueError:
            continue
        if _is_call(value, tool_names):
            return True
    return False


def _response_format(kind: ResponseFormat, name: str, fields: Collection[str]) -> dict[str, Any]:
    if kind == "json_object":
        return {"type": "json_object"}
    schema = {
        "type": "object",
        "properties": {f: {"type": "string"} for f in fields},
        "required": list(fields),
        "additionalProperties": False,
    }
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}


def _structured_tries(
    probe: str,
    model: BaseChatModel,
    variant: Variant,
    tries: int,
    prompt: str,
    schema_name: str,
    fields: Collection[str],
    exact: dict[str, str],
) -> ProbeResult:
    """Ask `tries` times for a JSON object with `fields`; ok = parseable object with them all.

    `counts["exact"]` is how many also copied every field in `exact` unchanged: a copying slip by
    the model is a finding about the model, not about serving, so it does not fail the probe.
    """
    bound = model.bind(
        response_format=_response_format(variant.response_format, schema_name, fields)
    )
    valid = right = errors = 0
    notes: list[str] = []
    for _ in range(tries):
        try:
            reply = bound.invoke([HumanMessage(prompt)])
        except SpendCapReached:
            raise
        except Exception as exc:
            errors += 1
            notes.append(f"{type(exc).__name__}: {str(exc)[:120]}")
            continue
        text = _text(reply.content)
        try:
            data = json.loads(text)
        except ValueError:
            notes.append(f"not JSON: {_clip(text)}")
            continue
        if not isinstance(data, dict) or not all(isinstance(data.get(f), str) for f in fields):
            notes.append(f"missing fields: {_clip(text)}")
            continue
        valid += 1
        if all(data[key] == value for key, value in exact.items()):
            right += 1
        else:
            notes.append(f"copied differently: {_clip(text)}")
    return ProbeResult(
        probe=probe,
        role=variant.role,
        response_format=variant.response_format,
        thinking=variant.thinking,
        tries=tries,
        ok=valid,
        counts={"valid": valid, "exact": right, "errors": errors},
        evidence=tuple(notes[:_EVIDENCE_LIMIT]),
    )


def escape_probe(model: BaseChatModel, variant: Variant, tries: int) -> ProbeResult:
    """Does a string with quotes, backslashes and a newline come back as valid JSON?"""
    prompt = (
        'Reply with a JSON object of the form {"text": <string>} and nothing else. The text '
        "must be exactly the following string, character for character:\n" + ESCAPE_SAMPLE
    )
    return _structured_tries(
        "escape", model, variant, tries, prompt, "escape", ["text"], {"text": ESCAPE_SAMPLE}
    )


def thinking_probe(model: BaseChatModel, variant: Variant, tries: int) -> ProbeResult:
    """With the variant's thinking setting (the safe-config question is `on`), is structured
    output still valid?"""
    prompt = (
        "Reply with a JSON object with the string fields person, origin and destination, and "
        f"nothing else, for this sentence: {EXTRACTION_TEXT}"
    )
    fields = list(EXTRACTION_FIELDS)
    return _structured_tries(
        "thinking", model, variant, tries, prompt, "extraction", fields, EXTRACTION_FIELDS
    )


def tool_call_probe(model: BaseChatModel, variant: Variant, tries: int) -> ProbeResult:
    """Does a request that needs a tool come back as a native tool call, not as text?"""
    bound = model.bind_tools([TOOL])
    native = as_text = no_call = errors = 0
    notes: list[str] = []
    for _ in range(tries):
        try:
            reply = bound.invoke([HumanMessage("What is the weather in Paris? Use the tool.")])
        except SpendCapReached:
            raise
        except Exception as exc:
            errors += 1
            notes.append(f"{type(exc).__name__}: {str(exc)[:120]}")
            continue
        calls = reply.tool_calls if isinstance(reply, AIMessage) else []
        text = _text(reply.content)
        if any(c["name"] == "get_weather" and "city" in c["args"] for c in calls):
            native += 1
        elif looks_like_tool_call_text(text, ["get_weather"]):
            as_text += 1
            notes.append(f"tool call as text: {_clip(text)}")
        else:
            no_call += 1
            notes.append(f"no tool call: {_clip(text)}")
    return ProbeResult(
        probe="tool_call",
        role=variant.role,
        response_format=variant.response_format,
        thinking=variant.thinking,
        tries=tries,
        ok=native,
        counts={"native": native, "as_text": as_text, "no_call": no_call, "errors": errors},
        evidence=tuple(notes[:_EVIDENCE_LIMIT]),
    )


def model_ids_probe(
    role: Role, configured_id: str | None, served: Collection[str] | None, error: str | None = None
) -> ProbeResult:
    """Is the id `config.yaml` names for `role` among the ids `/v1/models` serves?

    `served` is None when the listing failed; `error` then says why. Matching is exact.
    """
    if served is None:
        note = f"could not list models: {error}"
    elif configured_id is None:
        note = "config.yaml has no id for this role"
    elif configured_id not in served:
        note = f"{configured_id} is not served"
    else:
        note = ""
    ok = 0 if note else 1
    return ProbeResult(
        probe="model_ids",
        role=role,
        response_format=None,
        thinking=None,
        tries=1,
        ok=ok,
        counts={"served": ok},
        evidence=(note,) if note else (),
    )
