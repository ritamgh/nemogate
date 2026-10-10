"""`get_model(role)`: the only place a model client is built (AGENTS.md rule 1).

Not live (default): a deterministic `StubChatModel`; no network, no spend.
Live (`NEMOGATE_LIVE=1`): a `ChatOpenAI` on Chat Completions against Token Factory, with a
callback that reserves the call's estimated cost (refusing past the cap) before each request and
settles it at the real usage afterwards. SDK retries are off, so every HTTP request is one
reserve; a retry made above the model starts a new call and is reserved again.
"""

import json
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
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from common.env import REPO_ROOT, is_live, require_env
from common.spend import admit, append_spend, cost_usd
from schemas.config import AppConfig, ModelConfig, load_config
from schemas.enums import Role

API_KEY_ENV = "NEBIUS_API_KEY"
RESERVE_OUTPUT_TOKENS = 4096  # output tokens reserved per call, before the real usage is known
CHARS_PER_TOKEN = 3  # deliberately low (real text is ~4): over-estimating the input is safe


class ConfigError(RuntimeError):
    """A live call needs a config value that config.yaml leaves null."""


class UsageMissing(RuntimeError):
    """A live response carried no token counts, so its cost cannot be logged."""


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


class SpendHandler(BaseCallbackHandler):
    """Reserve the estimated cost before a request is sent; settle at the real usage after."""

    raise_error = True  # an exception here must stop the call, not just log a warning

    def __init__(self, role: Role, model_cfg: ModelConfig, config: AppConfig) -> None:
        self.role = role
        self.model_cfg = model_cfg
        self.config = config

    def on_chat_model_start(
        self, serialized: Any, messages: Any, *, run_id: Any, **kwargs: Any
    ) -> None:
        chars = sum(len(str(m.content)) for batch in messages for m in batch)
        params = kwargs.get("invocation_params") or {}
        tools = params.get("tools")
        if tools:
            chars += len(json.dumps(tools, default=str))
        admit(
            self.config,
            self.role,
            str(self.model_cfg.id),
            str(run_id),
            math.ceil(chars / CHARS_PER_TOKEN),
            max(RESERVE_OUTPUT_TOKENS, params.get("max_tokens") or 0),
            self.model_cfg,
        )

    def on_llm_end(self, response: LLMResult, *, run_id: Any, **kwargs: Any) -> None:
        input_tokens, output_tokens = self._usage(response)
        append_spend(
            self.role,
            str(self.model_cfg.id),
            input_tokens,
            output_tokens,
            cost_usd(input_tokens, output_tokens, self.model_cfg),
            call_id=str(run_id),
        )

    def _usage(self, response: LLMResult) -> tuple[int, int]:
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
        raise UsageMissing(
            f"The {self.role.value} response carried no token usage, so its cost cannot be "
            "logged. Its reserve stays counted against the cap."
        )


class _TokenFactoryChat(ChatOpenAI):
    """ChatOpenAI that never lets the API key out through a provider error message."""

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

    kwargs: dict[str, Any] = {}
    if model_cfg.temperature is not None:
        kwargs["temperature"] = model_cfg.temperature
    if http_client is not None:
        kwargs["http_client"] = http_client
    if http_async_client is not None:
        kwargs["http_async_client"] = http_async_client
    return _TokenFactoryChat(
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
