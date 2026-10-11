"""`get_model(role)`: the only place a model client is built (AGENTS.md rule 1).

Not live (default): a deterministic `StubChatModel`; no network, no spend.
Live (`NEMOGATE_LIVE=1`): a `ChatOpenAI` on Chat Completions against Token Factory, with a
callback that reserves the call's estimated cost (refusing past the cap) before each request and
settles it at the real usage afterwards. SDK retries are off, so every HTTP request is one
reserve; a retry made above the model starts a new call and is reserved again.
"""

import hashlib
import json
import logging
import math
import os
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
from langchain_core.callbacks import BaseCallbackHandler, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult, LLMResult
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from common.env import REPO_ROOT, is_live, require_env
from common.spend import admit, append_spend, cost_usd
from schemas.config import AppConfig, ModelConfig, SafeConfig, load_config
from schemas.enums import Role

logger = logging.getLogger(__name__)

API_KEY_ENV = "NEBIUS_API_KEY"
RESERVE_OUTPUT_TOKENS = 4096  # output tokens reserved per call, before the real usage is known
CHARS_PER_TOKEN = 3  # deliberately low (real text is ~4): over-estimating the input is safe


class ConfigError(RuntimeError):
    """A live call needs a config value that config.yaml leaves null."""


_ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


class StubChatModel(BaseChatModel):
    """Offline model: replays `responses` in order, else a fixed reply naming the role."""

    role: str
    responses: list[AIMessage] | None = None
    _next: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "nemogate-stub"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "StubChatModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.responses is None:
            reply = AIMessage(content=f"stub reply from {self.role}")
        elif self._next >= len(self.responses):
            raise RuntimeError(
                f"stub_responses exhausted after {len(self.responses)} replies "
                f"(role {self.role}): the code under test made one more model call than scripted"
            )
        else:
            reply = self.responses[self._next]
            self._next += 1
        if reply.usage_metadata is None:
            reply = reply.model_copy(update={"usage_metadata": dict(_ZERO_USAGE)})
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _message_chars(message: BaseMessage) -> int:
    """Characters a message adds to the prompt: its content plus any tool calls it carries.

    Tool-call arguments count (a `write_file` call carries the whole file), so a long history of
    them is not under-estimated.
    """
    chars = len(str(message.content))
    if isinstance(message, AIMessage):
        for call in message.tool_calls:
            chars += len(call["name"]) + len(json.dumps(call["args"], default=str))
        # ChatOpenAI sends these too, as the raw (unparsed) arguments string.
        for bad in message.invalid_tool_calls:
            chars += len(bad["name"] or "") + len(bad["args"] or "")
    return chars


class SpendHandler(BaseCallbackHandler):
    """Reserve the estimated cost before a request is sent; settle at the real usage after.

    A response with no token usage is settled at its reserve, so the cap never loses the call.
    """

    raise_error = True  # an exception here must stop the call, not just log a warning

    def __init__(self, role: Role, model_cfg: ModelConfig, config: AppConfig) -> None:
        self.role = role
        self.model_cfg = model_cfg
        self.config = config
        self._reserved: dict[str, tuple[int, int]] = {}  # run_id -> (input, output) tokens

    def on_chat_model_start(
        self, serialized: Any, messages: Any, *, run_id: Any, **kwargs: Any
    ) -> None:
        chars = sum(_message_chars(m) for batch in messages for m in batch)
        params = kwargs.get("invocation_params") or {}
        tools = params.get("tools")
        if tools:
            chars += len(json.dumps(tools, default=str))
        est_input = math.ceil(chars / CHARS_PER_TOKEN)
        est_output = max(RESERVE_OUTPUT_TOKENS, params.get("max_tokens") or 0)
        admit(
            self.config,
            self.role,
            str(self.model_cfg.id),
            str(run_id),
            est_input,
            est_output,
            self.model_cfg,
        )
        self._reserved[str(run_id)] = (est_input, est_output)

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        # The on-disk reserve stays counted (never settled); only the in-memory entry goes.
        self._reserved.pop(str(run_id), None)

    def on_llm_end(self, response: LLMResult, *, run_id: Any, **kwargs: Any) -> None:
        reserved = self._reserved.pop(str(run_id), None)
        usage = self._usage(response)
        if usage is None:
            if reserved is None:
                return  # no settle line: the reserve stays counted in full
            logger.warning(
                "The %s response carried no token usage; settled at its reserve "
                "(%d input + %d output tokens)",
                self.role.value,
                *reserved,
            )
            usage = reserved
        input_tokens, output_tokens = usage
        append_spend(
            self.role,
            str(self.model_cfg.id),
            input_tokens,
            output_tokens,
            cost_usd(input_tokens, output_tokens, self.model_cfg),
            call_id=str(run_id),
        )

    def _usage(self, response: LLMResult) -> tuple[int, int] | None:
        # Only the first generation: with n > 1 the response's one usage object is copied onto
        # every candidate, so summing them would bill the request n times.
        first = response.generations[0][0] if response.generations[0] else None
        usage = (
            first.message.usage_metadata
            if isinstance(first, ChatGeneration) and isinstance(first.message, AIMessage)
            else None
        )
        if usage:
            return usage["input_tokens"], usage["output_tokens"]
        token_usage = (response.llm_output or {}).get("token_usage") or {}
        if "prompt_tokens" in token_usage and "completion_tokens" in token_usage:
            return token_usage["prompt_tokens"], token_usage["completion_tokens"]
        return None


# safe_config.response_format -> the name ChatOpenAI.with_structured_output calls that method
_METHOD_FOR_RESPONSE_FORMAT = {"json_schema": "json_schema", "json_object": "json_mode"}


class _TokenFactoryChat(ChatOpenAI):
    """ChatOpenAI that never lets the API key out through a provider error message, and always
    sends the safe config's thinking switch and structured-output method."""

    _thinking: bool = PrivateAttr()
    _structured_method: str = PrivateAttr()

    def with_structured_output(
        self, schema: Any = None, *, method: Any = None, **kwargs: Any
    ) -> Runnable:
        # The base class defaults to function_calling, and a caller can ask for json_mode, which
        # Super answers with prose (docs/FACTS.md decision log). Only the safe config's method goes.
        configured = _METHOD_FOR_RESPONSE_FORMAT[self._structured_method]
        if method is not None and method != configured:
            raise ValueError(
                f"with_structured_output(method={method!r}) conflicts with the safe config's "
                f"{self._structured_method!r} ({configured!r}); omit method to use it"
            )
        return super().with_structured_output(schema, method=configured, **kwargs)

    def _get_request_payload(self, *args: Any, **kwargs: Any) -> dict:
        # Every call path builds its request here. A bound extra_body replaces the default one
        # wholesale, so the switch is merged in last, and the safe config wins over the caller.
        payload = super()._get_request_payload(*args, **kwargs)
        extra = dict(payload.get("extra_body") or {})
        # A response_format in either place reaches the wire (extra_body overrides the field), and
        # only the safe config's type may go. A request that sets none (chat, tools) is fine.
        for sent in (payload.get("response_format"), extra.get("response_format")):
            if sent is None:
                continue
            # A pydantic class here is turned into a json_schema format by the SDK.
            kind = sent.get("type") if isinstance(sent, dict) else "json_schema"
            if kind != self._structured_method:
                raise ValueError(
                    f"response_format type {kind!r} conflicts with the safe config's "
                    f"{self._structured_method!r}; only the configured one may be sent"
                )
        template = {**(extra.get("chat_template_kwargs") or {}), "enable_thinking": self._thinking}
        payload["extra_body"] = {**extra, "chat_template_kwargs": template}
        return payload

    def _redacted(self, exc: Exception) -> RuntimeError | None:
        key = self.openai_api_key.get_secret_value() if self.openai_api_key else ""
        text = f"{exc!s} {exc!r}"
        if not key or key not in text:
            return None
        return RuntimeError(f"{type(exc).__name__}: {str(exc).replace(key, '***')}")

    # Each override catches, then raises *after* the except block so the new error carries no
    # __context__ (the original exception, with the key in it, is dropped).

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        safe = None
        try:
            return super()._generate(*args, **kwargs)
        except Exception as exc:
            safe = self._redacted(exc)
            if safe is None:
                raise
        raise safe from None

    async def _agenerate(self, *args: Any, **kwargs: Any) -> ChatResult:
        safe = None
        try:
            return await super()._agenerate(*args, **kwargs)
        except Exception as exc:
            safe = self._redacted(exc)
            if safe is None:
                raise
        raise safe from None

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        safe = None
        try:
            yield from super()._stream(*args, **kwargs)
        except Exception as exc:
            safe = self._redacted(exc)
            if safe is None:
                raise
        if safe is not None:
            raise safe from None

    async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        safe = None
        try:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
        except Exception as exc:
            safe = self._redacted(exc)
            if safe is None:
                raise
        if safe is not None:
            raise safe from None


def _require(value: Any, field: str, role: Role) -> Any:
    if value is None:
        raise ConfigError(f"config.yaml has no {field} for role {role.value}; a live call needs it")
    return value


def safe_config_hash(safe: SafeConfig) -> str:
    """First 12 hex digits of sha256 over the safe config's two settings, as compact sorted JSON."""
    settings = {"response_format": safe.response_format, "thinking": safe.thinking}
    text = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def get_model(
    role: Role | str,
    *,
    stub_responses: Sequence[str | AIMessage] | None = None,
    config_path: Path | None = None,
    http_client: httpx.Client | None = None,
    http_async_client: httpx.AsyncClient | None = None,
) -> BaseChatModel:
    """Return the chat model for `role`: a stub unless NEMOGATE_LIVE=1.

    `http_client` / `http_async_client` exist only so tests can route the live path through an
    httpx MockTransport.
    """
    role = Role(role)
    if not is_live():
        replies = (
            None
            if stub_responses is None
            else [r if isinstance(r, AIMessage) else AIMessage(content=r) for r in stub_responses]
        )
        return StubChatModel(role=role.value, responses=replies)

    if stub_responses is not None:
        raise ValueError(
            "stub_responses given while NEMOGATE_LIVE=1: a test meant for the stub must never spend"
        )
    require_env(API_KEY_ENV)
    config = load_config(config_path or REPO_ROOT / "config.yaml")
    model_cfg = config.models[role]
    _require(model_cfg.id, "id", role)
    _require(model_cfg.input_usd_per_mtok, "input_usd_per_mtok", role)
    _require(model_cfg.output_usd_per_mtok, "output_usd_per_mtok", role)
    base_url = _require(config.token_factory.base_url, "token_factory.base_url", role)
    safe = config.safe_config
    response_format = _require(safe.response_format, "safe_config.response_format", role)
    thinking = _require(safe.thinking, "safe_config.thinking", role)
    stored_hash = _require(safe.hash, "safe_config.hash", role)
    if stored_hash != safe_config_hash(safe):
        raise ConfigError(
            f"config.yaml safe_config.hash is {stored_hash} but the settings hash to "
            f"{safe_config_hash(safe)}; update the hash with the settings"
        )

    kwargs: dict[str, Any] = {}
    if model_cfg.temperature is not None:
        kwargs["temperature"] = model_cfg.temperature
    if http_client is not None:
        kwargs["http_client"] = http_client
    if http_async_client is not None:
        kwargs["http_async_client"] = http_async_client
    model = _TokenFactoryChat(
        model=model_cfg.id,
        base_url=base_url,
        api_key=os.environ[API_KEY_ENV],
        use_responses_api=False,
        stream_usage=True,
        timeout=120,
        max_retries=0,  # one HTTP request per reserve; the SDK must not resend on its own
        callbacks=[SpendHandler(role, model_cfg, config)],
        **kwargs,
    )
    # Thinking is on by default on Token Factory (docs/FACTS.md section 2); always send the switch.
    model._thinking = thinking
    model._structured_method = response_format
    return model
