"""The live path, exercised only through an httpx MockTransport (no network, no credit)."""

import asyncio
import json
import logging

import httpx
import pytest

from common import ConfigError, MissingEnvError, SpendCapReached, UsageMissing
from common.llm import get_model
from common.spend import append_spend, run_scope
from common.tests.conftest import FAKE_KEY, Transport, completion, read_log, write_config

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


def test_one_call_logs_exactly_one_hand_computed_line(live, config_path, spend_log):
    # Breaks if cost isn't in*price_in/1e6 + out*price_out/1e6 (1000*1 + 500*3 per Mtok = 0.0025).
    transport = Transport()
    model = get_model("sut", config_path=config_path, **transport.clients)
    reply = model.invoke("hi")
    assert reply.content == "hello"
    assert len(transport.requests) == 1
    (line,) = read_log(spend_log)
    assert line["role"] == "sut"
    assert line["model_id"] == "m-sut"
    assert (line["input_tokens"], line["output_tokens"]) == (1000, 500)
    assert line["cost_usd"] == pytest.approx(0.0025)
    assert line["run_id"] is None


def test_ainvoke_logs_one_line(live, config_path, spend_log):
    # Breaks if the handler only fires on the sync path (Deep Agents may call async).
    transport = Transport()
    model = get_model("sut", config_path=config_path, **transport.clients)
    asyncio.run(model.ainvoke("hi"))
    (line,) = read_log(spend_log)
    assert line["cost_usd"] == pytest.approx(0.0025)


def test_bind_tools_wrapped_model_still_logs(live, config_path, spend_log):
    # Breaks if callbacks are lost when the model is wrapped by bind_tools.
    transport = Transport()
    model = get_model("sut", config_path=config_path, **transport.clients)
    model.bind_tools([TOOL]).invoke("hi")
    asyncio.run(model.bind_tools([TOOL]).ainvoke("hi"))
    lines = read_log(spend_log)
    assert len(lines) == 2
    assert [line["cost_usd"] for line in lines] == [pytest.approx(0.0025)] * 2


def test_role_picks_its_own_price_and_temperature(live, config_path, spend_log):
    # Breaks if every role is billed at one price, or temperature is dropped / sent when null.
    helper_t = Transport()
    get_model("helper", config_path=config_path, **helper_t.clients).invoke("hi")
    sut_t = Transport()
    get_model("sut", config_path=config_path, **sut_t.clients).invoke("hi")
    helper_line, _ = read_log(spend_log)
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
    # Breaks if the handler loses the run_id context (including in the async path).
    transport = Transport()
    model = get_model("sut", config_path=config_path, **transport.clients)
    with run_scope("r-1"):
        model.invoke("hi")
        asyncio.run(model.ainvoke("hi"))
    assert [line["run_id"] for line in read_log(spend_log)] == ["r-1", "r-1"]


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


def test_just_below_the_cap_the_call_goes_through(live, config_path, spend_log):
    # Breaks if the cap trips early (7.99 of 8.0 spent).
    append_spend("sut", "m", 1, 1, 7.99)
    transport = Transport()
    get_model("sut", config_path=config_path, **transport.clients).invoke("hi")
    assert len(transport.requests) == 1
    assert len(read_log(spend_log)) == 2


def test_response_without_usage_is_loud(live, config_path, spend_log):
    # Breaks if a response with no token counts is logged as free, undercounting the cap.
    transport = Transport(completion(prompt_tokens=None))
    model = get_model("sut", config_path=config_path, **transport.clients)
    with pytest.raises(UsageMissing):
        model.invoke("hi")
    assert read_log(spend_log) == []


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


def test_spend_cap_error_message_has_no_key(live, config_path, spend_log, caplog):
    # Breaks if SpendCapReached text or its callback-warning log carries the key.
    append_spend("sut", "m", 1, 1, 9.0)
    model = get_model("sut", config_path=config_path, **Transport().clients)
    with caplog.at_level(logging.DEBUG), pytest.raises(SpendCapReached) as exc:
        model.invoke("hi")
    assert FAKE_KEY not in str(exc.value) + caplog.text
