import json

import pytest
from langchain_core.messages import AIMessage

from common import SpendCapReached, get_model
from preflight.probes import (
    ESCAPE_SAMPLE,
    Variant,
    escape_probe,
    looks_like_tool_call_text,
    model_ids_probe,
    thinking_probe,
    tool_call_probe,
)
from schemas.enums import Role

SCHEMA_OFF = Variant(Role.sut, "json_schema", False)
SCHEMA_ON = Variant(Role.sut, "json_schema", True)


def _text_reply(text: str) -> str:
    return json.dumps({"text": text})


def _native_call(city: str = "Paris") -> AIMessage:
    call = {"name": "get_weather", "args": {"city": city}, "id": "call_1", "type": "tool_call"}
    return AIMessage(content="", tool_calls=[call])


def test_escape_probe_counts_valid_json_and_exact_copies_separately():
    # Breaks if a corrupted string (dropped backslash) is counted as an exact copy, or if prose
    # instead of JSON (Super under json_object) is counted as valid.
    corrupted = ESCAPE_SAMPLE.replace("\\", "")
    model = get_model(
        "sut",
        stub_responses=[_text_reply(ESCAPE_SAMPLE), _text_reply(corrupted), "Let me think..."],
    )
    result = escape_probe(model, Variant(Role.sut, "json_object", False), tries=3)
    assert result.counts == {"valid": 2, "exact": 1, "errors": 0}
    assert result.ok == 2
    assert not result.passed
    assert result.evidence[0].startswith("copied differently: ")
    assert result.evidence[1] == "not JSON: 'Let me think...'"


def test_escape_probe_passes_on_valid_json_even_when_the_copy_is_off():
    # Breaks if a copying slip by the model (a finding about the model, docs/FACTS.md section 6)
    # fails the serving probe: at temperature 1.0 Super copies it exactly only about half the time.
    model = get_model("sut", stub_responses=[_text_reply("He said hi")] * 2)
    result = escape_probe(model, SCHEMA_OFF, tries=2)
    assert result.counts == {"valid": 2, "exact": 0, "errors": 0}
    assert result.passed


def test_escape_probe_rejects_json_without_the_field():
    # Breaks if any JSON object counts as a valid structured answer.
    model = get_model("sut", stub_responses=['{"answer": "x"}'])
    result = escape_probe(model, SCHEMA_OFF, tries=1)
    assert result.counts == {"valid": 0, "exact": 0, "errors": 0}
    assert result.evidence == ('missing fields: \'{"answer": "x"}\'',)


def test_escape_probe_counts_a_failing_call_as_an_error_not_a_crash():
    # Breaks if one HTTP error aborts the whole preflight instead of failing that probe.
    model = get_model("sut", stub_responses=[_text_reply(ESCAPE_SAMPLE)])  # the 2nd call raises
    result = escape_probe(model, SCHEMA_OFF, tries=2)
    assert result.counts == {"valid": 1, "exact": 1, "errors": 1}
    assert not result.passed


def test_the_spend_cap_is_not_swallowed_as_a_probe_failure(monkeypatch):
    # Breaks if hitting the cap reads as "the model failed" and the run carries on spending.
    model = get_model("sut", stub_responses=["x"])

    def refuse(*args, **kwargs):
        raise SpendCapReached("cap")

    monkeypatch.setattr(type(model), "_generate", refuse)
    with pytest.raises(SpendCapReached):
        escape_probe(model, SCHEMA_OFF, tries=1)


def test_the_json_object_prompt_asks_for_json_in_words(monkeypatch):
    # Breaks if the json_object request omits the word JSON, which the API rejects.
    seen = []
    model = get_model("sut", stub_responses=[_text_reply(ESCAPE_SAMPLE)])
    original = type(model)._generate

    def spy(self, messages, *args, **kwargs):
        seen.append((messages[0].content, kwargs.get("response_format")))
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(type(model), "_generate", spy)
    escape_probe(model, Variant(Role.sut, "json_object", False), tries=1)
    prompt, response_format = seen[0]
    assert "JSON" in prompt
    assert response_format == {"type": "json_object"}


@pytest.mark.parametrize(
    "content",
    [
        '{"name": "get_weather", "arguments": {"city": "Paris"}}',
        'Sure.\n```json\n{"name": "get_weather", "parameters": {"city": "Paris"}}\n```',
        '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>',
        '{"type": "function", "function": {"name": "get_weather", "arguments": "{}"}}',
        '{"tool_calls": [{"name": "get_weather", "args": {"city": "Paris"}}]}',
        "<function=get_weather><parameter=city>Paris</parameter></function>",
    ],
)
def test_a_tool_call_written_as_text_is_flagged(content):
    # Breaks if a tool call serialized into content (NAT #2274 class) passes as an answer.
    assert looks_like_tool_call_text(content, ["get_weather"])


@pytest.mark.parametrize(
    "content",
    [
        "I would call get_weather with city Paris, but I cannot right now.",
        "It is sunny in Paris.",
        '{"city": "Paris", "temp_c": 12}',
        '{"name": "Alice", "arguments": 3}',
        '{"name": "other_tool", "arguments": {}}',
        "a set {1, 2} and a list [1, 2]",
        "",
    ],
)
def test_prose_and_ordinary_json_are_not_flagged(content):
    # Breaks if the detector cries wolf: a reply about the tool, or JSON data with a `name` key
    # for something that is not one of the offered tools.
    assert not looks_like_tool_call_text(content, ["get_weather"])


def test_without_tool_names_any_name_plus_arguments_object_is_flagged():
    # Breaks if the detector needs the tool list to catch the obvious shape.
    assert looks_like_tool_call_text('{"name": "x", "arguments": {}}')


def test_tool_call_probe_separates_native_text_and_no_call():
    # Breaks if a tool call as text counts as native, or if a refusal is mistaken for a call.
    text_call = AIMessage(content='{"name": "get_weather", "arguments": {"city": "Paris"}}')
    model = get_model(
        "sut", stub_responses=[_native_call(), text_call, AIMessage(content="No idea.")]
    )
    result = tool_call_probe(model, SCHEMA_OFF, tries=3)
    assert result.counts == {"native": 1, "as_text": 1, "no_call": 1, "errors": 0}
    assert result.ok == 1
    assert not result.passed
    assert result.evidence[0].startswith("tool call as text: ")


def test_tool_call_probe_passes_when_every_call_is_native():
    # Breaks if native calls with the right name and arguments are not recognized.
    model = get_model("sut", stub_responses=[_native_call(), _native_call("Oslo")])
    assert tool_call_probe(model, SCHEMA_OFF, tries=2).passed


def test_a_native_call_to_the_wrong_tool_is_not_native():
    # Breaks if any tool_calls entry counts, even for a tool that was never offered.
    wrong = AIMessage(
        content="",
        tool_calls=[{"name": "delete_all", "args": {}, "id": "c", "type": "tool_call"}],
    )
    model = get_model("sut", stub_responses=[wrong])
    result = tool_call_probe(model, SCHEMA_OFF, tries=1)
    assert result.counts["native"] == 0
    assert result.counts["no_call"] == 1


def test_thinking_probe_needs_every_field_and_reports_correct_ones():
    # Breaks if a thinking-on reply with a missing field counts as valid structured output.
    full = json.dumps({"person": "Dana", "origin": "Oslo", "destination": "Lima"})
    wrong = json.dumps({"person": "Dana", "origin": "Lima", "destination": "Oslo"})
    partial = json.dumps({"person": "Dana"})
    model = get_model("sut", stub_responses=[full, wrong, partial])
    result = thinking_probe(model, SCHEMA_ON, tries=3)
    assert result.counts == {"valid": 2, "exact": 1, "errors": 0}
    assert result.thinking is True
    assert not result.passed


def test_model_ids_probe_names_the_missing_id():
    # Breaks if a configured id that is not served (e.g. a wrong capital letter) passes.
    served = {"nvidia/Nemotron-3-Ultra-550b-a55b"}
    ok = model_ids_probe(Role.planner, "nvidia/Nemotron-3-Ultra-550b-a55b", served)
    bad = model_ids_probe(Role.planner, "nvidia/nemotron-3-ultra-550b-a55b", served)
    assert ok.passed
    assert not bad.passed
    assert bad.evidence == ("nvidia/nemotron-3-ultra-550b-a55b is not served",)


def test_model_ids_probe_fails_when_the_listing_failed():
    # Breaks if an unreachable /v1/models counts as "all ids confirmed".
    result = model_ids_probe(Role.sut, "m", None, "GET x answered 500")
    assert not result.passed
    assert result.evidence == ("could not list models: GET x answered 500",)
