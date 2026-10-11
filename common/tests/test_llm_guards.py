"""Guards in the live path: structured-output method, missing usage, tool-call estimate."""

import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

from common.llm import get_model
from common.spend import total_spent
from common.tests.conftest import Transport, completion, read_kind, read_log

# Reserve for the prompt "hi" on sut (1.00 in / 3.00 out per Mtok): 1 input token + 4096 output.
HI_RESERVE_USD = 0.012289


class Answer(BaseModel):
    value: int


def _json_reply() -> dict:
    body = completion()
    body["choices"][0]["message"]["content"] = '{"value": 7}'
    return body


def test_structured_output_defaults_to_the_safe_configs_json_schema(live, config_path):
    # Breaks if get_model stops applying safe_config.response_format: the base class would then
    # default to function_calling (tools in the request, no response_format).
    transport = Transport(_json_reply())
    model = get_model("sut", config_path=config_path, **transport.clients)
    assert model.with_structured_output(Answer).invoke("hi") == Answer(value=7)
    body = transport.requests[0].read().decode().replace(" ", "")
    assert '"type":"json_schema"' in body
    assert '"tools"' not in body


def test_a_conflicting_structured_output_method_is_refused(live, config_path):
    # Breaks if a caller can pick json_mode (json_object), which Super answers with prose
    # (the C0 result), instead of the configured json_schema.
    transport = Transport(_json_reply())
    model = get_model("sut", config_path=config_path, **transport.clients)
    with pytest.raises(ValueError, match="json_mode.*json_schema"):
        model.with_structured_output(Answer, method="json_mode")
    with pytest.raises(ValueError, match="function_calling.*json_schema"):
        model.with_structured_output(Answer, method="function_calling")
    assert transport.requests == []


def test_response_without_usage_settles_at_its_reserve_and_returns_the_reply(
    live, config_path, spend_log, caplog
):
    # Breaks if a paid reply with no token counts is thrown away (the old UsageMissing raise) or
    # if the reserve is released as zero: it must settle at the reserve and warn.
    transport = Transport(completion(prompt_tokens=None))
    model = get_model("sut", config_path=config_path, **transport.clients)
    with caplog.at_level(logging.WARNING, logger="common.llm"):
        reply = model.invoke("hi")
    assert reply.content == "hello"
    reserve, settle = read_log(spend_log)
    assert (reserve["kind"], settle["kind"]) == ("reserve", "settle")
    assert settle["call_id"] == reserve["call_id"]
    assert (settle["input_tokens"], settle["output_tokens"]) == (1, 4096)
    assert settle["cost_usd"] == pytest.approx(HI_RESERVE_USD)
    assert total_spent() == pytest.approx(HI_RESERVE_USD)
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "The sut response carried no token usage; settled at its reserve "
        "(1 input + 4096 output tokens)"
    ]


def test_tool_call_arguments_in_the_history_are_counted(live, config_path, spend_log):
    # Breaks if the estimate counts only message content: the 3000-char write_file argument
    # would be missed, leaving 2 tokens instead of 1010.
    # chars: "hi" 2 + "ok" 2 + "write_file" 10 + '{"content": "' 13 + 3000 + '"}' 2 = 3029
    history = [
        HumanMessage("hi"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"content": "x" * 3000},
                    "id": "c1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage("ok", tool_call_id="c1"),
    ]
    transport = Transport()
    get_model("sut", config_path=config_path, **transport.clients).invoke(history)
    (reserve,) = read_kind(spend_log, "reserve")
    assert reserve["est_input_tokens"] == 1010
