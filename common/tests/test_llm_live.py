"""The live path, exercised only through an httpx MockTransport (no network, no credit)."""

import asyncio
import json
import logging

import httpx
import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from common import ConfigError, MissingEnvError, SpendCapReached, UsageMissing
from common.llm import get_model
from common.spend import append_spend, run_scope, total_spent
from common.tests.conftest import (
    FAKE_KEY,
    Transport,
    chunk,
    completion,
    read_kind,
    read_log,
    sse,
    write_config,
)

# Reserve for the prompt "hi" on sut (1.00 in / 3.00 out per Mtok): 1 input token (2 chars / 3,
# rounded up) + 4096 reserved output tokens = 1*1/1e6 + 4096*3/1e6.
HI_RESERVE_USD = 0.012289

USAGE = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}

TOOL = {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}


def test_live_with_stub_responses_is_refused_and_sends_nothing(live, config_path):
    # Breaks if a stub-only test could ever reach the paid path.
    transport = Transport()
    with pytest.raises(ValueError, match="stub_responses"):
        get_model("sut", stub_responses=["x"], config_path=config_path, **transport.clients)
    assert transport.requests == []


@pytest.mark.parametrize("value", [None, "", "   "])
def test_live_without_a_key_names_the_variable(monkeypatch, config_path, value):
    # Breaks if a missing/blank key is passed to the client (it would fail late, as 401).
    monkeypatch.setenv("NEMOGATE_LIVE", "1")
    if value is not None:
        monkeypatch.setenv("NEBIUS_API_KEY", value)
    with pytest.raises(MissingEnvError, match="NEBIUS_API_KEY"):
        get_model("sut", config_path=config_path)


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ("models__sut__id", "id"),
        ("models__sut__input_usd_per_mtok", "input_usd_per_mtok"),
        ("models__sut__output_usd_per_mtok", "output_usd_per_mtok"),
        ("token_factory__base_url", "base_url"),
    ],
)
def test_live_with_a_null_field_raises_config_error_naming_it(live, tmp_path, override, field):
    # Breaks if a null price silently becomes zero cost, or the error doesn't say which field.
    path = write_config(tmp_path / "null.yaml", **{override: None})
    with pytest.raises(ConfigError, match=field):
        get_model("sut", config_path=path)


def test_one_call_logs_a_reserve_then_a_settle_with_the_same_call_id(live, config_path, spend_log):
    # Breaks if cost isn't in*price_in/1e6 + out*price_out/1e6 (1000*1 + 500*3 per Mtok = 0.0025),
    # or the call is not reserved before it is sent and settled after it.
    transport = Transport()
    model = get_model("sut", config_path=config_path, **transport.clients)
    reply = model.invoke("hi")
    assert reply.content == "hello"
    assert len(transport.requests) == 1
    (reserve,) = read_kind(spend_log, "reserve")
    (settle,) = read_kind(spend_log, "settle")
    assert [line["kind"] for line in read_log(spend_log)] == ["reserve", "settle"]
    assert reserve["call_id"] == settle["call_id"]
    assert reserve["run_id"] is None and settle["run_id"] is None
    assert (reserve["est_input_tokens"], reserve["est_output_tokens"]) == (1, 4096)
    assert reserve["cost_usd"] == pytest.approx(HI_RESERVE_USD)
    assert (settle["role"], settle["model_id"]) == ("sut", "m-sut")
    assert (settle["input_tokens"], settle["output_tokens"]) == (1000, 500)
    assert settle["cost_usd"] == pytest.approx(0.0025)
    assert total_spent() == pytest.approx(0.0025)


def test_the_reserve_counts_message_text_and_bound_tools(live, config_path, spend_log):
    # Breaks if the estimate ignores the prompt or the tool schemas: a 3000-char prompt reserves
    # 1000 input tokens, and the same prompt with a tool bound must reserve more.
    transport = Transport()
    model = get_model("sut", config_path=config_path, **transport.clients)
    model.invoke("x" * 3000)
    model.bind_tools([TOOL]).invoke("x" * 3000)
    plain, with_tool = read_kind(spend_log, "reserve")
    assert plain["est_input_tokens"] == 1000
    assert with_tool["est_input_tokens"] > 1000


def test_ainvoke_logs_one_settle(live, config_path, spend_log):
    # Breaks if the handler only fires on the sync path (Deep Agents may call async).
    transport = Transport()
    model = get_model("sut", config_path=config_path, **transport.clients)
    asyncio.run(model.ainvoke("hi"))
    (settle,) = read_kind(spend_log, "settle")
    assert settle["cost_usd"] == pytest.approx(0.0025)
    assert len(read_kind(spend_log, "reserve")) == 1


def test_bind_tools_wrapped_model_still_logs(live, config_path, spend_log):
    # Breaks if callbacks are lost when the model is wrapped by bind_tools.
    transport = Transport()
    model = get_model("sut", config_path=config_path, **transport.clients)
    model.bind_tools([TOOL]).invoke("hi")
    asyncio.run(model.bind_tools([TOOL]).ainvoke("hi"))
    settles = read_kind(spend_log, "settle")
    assert len(settles) == 2
    assert [line["cost_usd"] for line in settles] == [pytest.approx(0.0025)] * 2


def test_role_picks_its_own_price_and_temperature(live, config_path, spend_log):
    # Breaks if every role is billed at one price, or temperature is dropped / sent when null.
    helper_t = Transport()
    get_model("helper", config_path=config_path, **helper_t.clients).invoke("hi")
    sut_t = Transport()
    get_model("sut", config_path=config_path, **sut_t.clients).invoke("hi")
    helper_line, _ = read_kind(spend_log, "settle")
    assert helper_line["model_id"] == "m-helper"
    assert helper_line["cost_usd"] == pytest.approx(0.0015)  # 1000*0.5 + 500*2 per Mtok
    helper_body = json.loads(helper_t.requests[0].content)
    sut_body = json.loads(sut_t.requests[0].content)
    assert helper_body["model"] == "m-helper"
    assert helper_body["temperature"] == 0
    assert "temperature" not in sut_body
    assert str(helper_t.requests[0].url) == "https://tf.example.invalid/v1/chat/completions"
    assert helper_t.requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"


def test_run_scope_tags_live_lines(live, config_path, spend_log):
    # Breaks if the handler loses the run_id context (including in the async path), on either
    # the reserve or the settle line.
    transport = Transport()
    model = get_model("sut", config_path=config_path, **transport.clients)
    with run_scope("r-1"):
        model.invoke("hi")
        asyncio.run(model.ainvoke("hi"))
    lines = read_log(spend_log)
    assert [line["kind"] for line in lines] == ["reserve", "settle", "reserve", "settle"]
    assert [line["run_id"] for line in lines] == ["r-1"] * 4


def test_at_the_cap_no_request_is_sent(live, config_path, spend_log):
    # Breaks if the cap is checked after the HTTP call instead of before it (cap = 10 * 0.8).
    append_spend("sut", "m", 1, 1, 8.0)
    transport = Transport()
    model = get_model("sut", config_path=config_path, **transport.clients)
    with pytest.raises(SpendCapReached):
        model.invoke("hi")
    with pytest.raises(SpendCapReached):
        asyncio.run(model.ainvoke("hi"))
    with pytest.raises(SpendCapReached):
        model.bind_tools([TOOL]).invoke("hi")
    assert transport.requests == []
    assert len(read_log(spend_log)) == 1


def test_a_call_whose_reserve_would_cross_the_cap_is_refused(live, config_path, spend_log):
    # Breaks if only spent-so-far is compared to the cap: 7.995 spent + 0.012289 reserve > 8.0,
    # so the call must not be sent even though 7.995 < 8.0.
    append_spend("sut", "m", 1, 1, 7.995)
    transport = Transport()
    model = get_model("sut", config_path=config_path, **transport.clients)
    with pytest.raises(SpendCapReached):
        model.invoke("hi")
    assert transport.requests == []
    assert len(read_log(spend_log)) == 1  # no reserve was written for the refused call


def test_well_below_the_cap_the_call_goes_through(live, config_path, spend_log):
    # Breaks if the reserve check trips early (7.9 spent + 0.012289 reserve < 8.0).
    append_spend("sut", "m", 1, 1, 7.9)
    transport = Transport()
    get_model("sut", config_path=config_path, **transport.clients).invoke("hi")
    assert len(transport.requests) == 1
    assert total_spent() == pytest.approx(7.9025)


def test_response_without_usage_stays_counted_at_its_reserve(live, config_path, spend_log):
    # Breaks if a response with no token counts leaves the ledger empty (fail-open): the paid
    # call must stay counted at its reserve, and a retry must see it.
    transport = Transport(completion(prompt_tokens=None))
    model = get_model("sut", config_path=config_path, **transport.clients)
    with pytest.raises(UsageMissing):
        model.invoke("hi")
    assert [line["kind"] for line in read_log(spend_log)] == ["reserve"]
    assert total_spent() == pytest.approx(HI_RESERVE_USD)
    with pytest.raises(UsageMissing):
        model.invoke("hi")
    assert total_spent() == pytest.approx(2 * HI_RESERVE_USD)


def test_http_error_leaves_the_reserve_counted_and_is_not_retried(live, config_path, spend_log):
    # Breaks if the SDK retries a failed request on its own (extra paid requests that skip the
    # cap check), or a failed call drops out of the total.
    transport = Transport(lambda request: httpx.Response(500, json={"error": "boom"}))
    model = get_model("sut", config_path=config_path, **transport.clients)
    with pytest.raises(Exception, match="boom"):
        model.invoke("hi")
    assert len(transport.requests) == 1
    assert total_spent() == pytest.approx(HI_RESERVE_USD)


def test_every_new_attempt_makes_its_own_reserve(live, config_path, spend_log):
    # Breaks if a caller-level retry reuses one reserve: each model call must be admitted anew.
    transport = Transport(lambda request: httpx.Response(500, json={"error": "boom"}))
    model = get_model("sut", config_path=config_path, **transport.clients)
    for _ in range(3):
        with pytest.raises(Exception, match="boom"):
            model.invoke("hi")
    assert len(read_kind(spend_log, "reserve")) == 3
    assert total_spent() == pytest.approx(3 * HI_RESERVE_USD)


def _stream_reply(request: httpx.Request) -> httpx.Response:
    return sse(chunk("he"), chunk("llo", finish="stop"), chunk(None, usage=USAGE))


def test_an_interrupted_stream_stays_counted_at_its_reserve(live, config_path, spend_log):
    # Breaks if a stream the caller abandons after one chunk leaves no trace in the total.
    transport = Transport(_stream_reply)
    model = get_model("sut", config_path=config_path, **transport.clients)
    stream = model.stream("hi")
    next(stream)
    stream.close()
    assert [line["kind"] for line in read_log(spend_log)] == ["reserve"]
    assert total_spent() == pytest.approx(HI_RESERVE_USD)


def test_a_completed_stream_settles_at_its_real_usage(live, config_path, spend_log):
    # Breaks if streamed calls are never settled (stream_usage chunk ignored): the cost must be
    # 1000*1 + 500*3 per Mtok = 0.0025, not the reserve.
    transport = Transport(_stream_reply)
    model = get_model("sut", config_path=config_path, **transport.clients)
    text = "".join(c.content for c in model.stream("hi"))
    assert text == "hello"
    assert [line["kind"] for line in read_log(spend_log)] == ["reserve", "settle"]
    assert total_spent() == pytest.approx(0.0025)


def test_an_interrupted_async_stream_stays_counted_at_its_reserve(live, config_path, spend_log):
    # Breaks if the async stream path skips the reserve/cap accounting.
    transport = Transport(_stream_reply)
    model = get_model("sut", config_path=config_path, **transport.clients)

    async def first_chunk_only():
        agen = model.astream("hi")
        await agen.__anext__()
        await agen.aclose()

    asyncio.run(first_chunk_only())
    assert [line["kind"] for line in read_log(spend_log)] == ["reserve"]
    assert total_spent() == pytest.approx(HI_RESERVE_USD)


def test_structured_output_that_fails_validation_stays_counted(live, config_path, spend_log):
    # Breaks if a response that carries usage but fails SDK/schema validation is logged as free.
    class Answer(BaseModel):
        value: int

    transport = Transport()  # the canned reply is the text "hello", not JSON for Answer
    model = get_model("sut", config_path=config_path, **transport.clients)
    structured = model.with_structured_output(Answer, method="json_schema")
    with pytest.raises(Exception):  # noqa: B017 - the validation error type is the SDK's choice
        structured.invoke("hi")
    assert len(transport.requests) == 1
    assert [line["kind"] for line in read_log(spend_log)] == ["reserve"]
    assert total_spent() == pytest.approx(HI_RESERVE_USD)


def test_several_candidates_are_billed_once_not_per_candidate(live, config_path, spend_log):
    # Breaks if usage is summed across generations: with n=2 the response's one usage object
    # (1000 in / 500 out) is copied onto each candidate, and must be billed once (0.0025).
    body = completion()
    body["choices"].append(
        {"index": 1, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    )
    transport = Transport(body)
    model = get_model("sut", config_path=config_path, **transport.clients)
    result = model.generate([[HumanMessage("hi")]], n=2)
    assert len(result.generations[0]) == 2
    (settle,) = read_kind(spend_log, "settle")
    assert (settle["input_tokens"], settle["output_tokens"]) == (1000, 500)
    assert settle["cost_usd"] == pytest.approx(0.0025)


def test_key_never_appears_in_repr_exceptions_or_logs(live, config_path, caplog, capsys):
    # Breaks if the key leaks via repr(model), an API error message, or the callback warning log.
    transport = Transport(lambda request: httpx.Response(401, json={"error": "no"}))
    model = get_model("sut", config_path=config_path, **transport.clients)
    assert FAKE_KEY not in repr(model)
    assert FAKE_KEY not in str(model)
    with caplog.at_level(logging.DEBUG), pytest.raises(Exception) as exc:
        model.invoke("hi")
    assert FAKE_KEY not in str(exc.value) + repr(exc.value)
    out = capsys.readouterr()
    assert FAKE_KEY not in out.out + out.err + caplog.text


def _echo_key(request: httpx.Request) -> httpx.Response:
    # What a provider does on a bad key: repeat the submitted credential in the error text.
    submitted = request.headers["authorization"].removeprefix("Bearer ")
    return httpx.Response(401, json={"error": {"message": f"Invalid API key: {submitted}"}})


def _chain(exc: BaseException | None):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


def _invoke(model):
    model.invoke("hi")


def _ainvoke(model):
    asyncio.run(model.ainvoke("hi"))


def _stream(model):
    list(model.stream("hi"))


def _astream(model):
    async def drain():
        return [c async for c in model.astream("hi")]

    asyncio.run(drain())


@pytest.mark.parametrize("call", [_invoke, _ainvoke, _stream, _astream])
def test_a_provider_error_that_echoes_the_key_is_redacted(
    live, config_path, caplog, capsys, spend_log, call
):
    # Breaks if an API error body that repeats the credential reaches the caller, its chained
    # exceptions, the logs or the stream output (invoke, ainvoke, stream and astream paths).
    transport = Transport(_echo_key)
    model = get_model("sut", config_path=config_path, **transport.clients)
    with caplog.at_level(logging.DEBUG), pytest.raises(Exception) as exc:
        call(model)
    for link in _chain(exc.value):
        assert FAKE_KEY not in str(link) + repr(link)
    assert "Invalid API key: ***" in str(exc.value)
    out = capsys.readouterr()
    assert FAKE_KEY not in out.out + out.err + caplog.text + spend_log.read_text()


def test_spend_cap_error_message_has_no_key(live, config_path, spend_log, caplog):
    # Breaks if SpendCapReached text or its callback-warning log carries the key.
    append_spend("sut", "m", 1, 1, 9.0)
    model = get_model("sut", config_path=config_path, **Transport().clients)
    with caplog.at_level(logging.DEBUG), pytest.raises(SpendCapReached) as exc:
        model.invoke("hi")
    assert FAKE_KEY not in str(exc.value) + caplog.text
