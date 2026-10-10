"""Shared fixtures. Nothing here touches the real `.env`, `runs/` or the network."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import yaml

import common.env

FAKE_KEY = "fake-key-sentinel-7f3a91c2"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # Breaks if a test reads the developer's real .env or the real spend log.
    monkeypatch.setattr(common.env, "REPO_ROOT", tmp_path / "no-repo-root")
    monkeypatch.delenv("NEMOGATE_LIVE", raising=False)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.setenv("NEMOGATE_SPEND_LOG", str(tmp_path / "spend.jsonl"))


@pytest.fixture
def spend_log(tmp_path) -> Path:
    return tmp_path / "spend.jsonl"


def write_config(path: Path, **overrides) -> Path:
    """A tmp config with simple literal prices: sut 1.00/3.00, helper 0.50/2.00, planner 2/6."""
    cfg = {
        "token_factory": {"base_url": "https://tf.example.invalid/v1"},
        "models": {
            "helper": {
                "id": "m-helper",
                "input_usd_per_mtok": 0.5,
                "output_usd_per_mtok": 2.0,
                "temperature": 0,
                "context_window": 1000,
            },
            "sut": {
                "id": "m-sut",
                "input_usd_per_mtok": 1.0,
                "output_usd_per_mtok": 3.0,
                "temperature": None,
                "context_window": 1000,
            },
            "planner": {
                "id": "m-planner",
                "input_usd_per_mtok": 2.0,
                "output_usd_per_mtok": 6.0,
                "temperature": 0,
                "context_window": 1000,
            },
        },
        "safe_config": {"response_format": None, "thinking": None, "hash": None},
        "budget": {"total_usd": 10, "stop_at_fraction": 0.8},
    }
    for dotted, value in overrides.items():
        node = cfg
        *parents, leaf = dotted.split("__")
        for key in parents:
            node = node[key]
        node[leaf] = value
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


@pytest.fixture
def config_path(tmp_path) -> Path:
    return write_config(tmp_path / "config.yaml")


def completion(prompt_tokens: int | None = 1000, completion_tokens: int | None = 500) -> dict:
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
    }
    if prompt_tokens is not None:
        body["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return body


class Transport:
    """An httpx MockTransport that counts requests and replies with a canned completion."""

    def __init__(self, body: dict | Callable[[httpx.Request], httpx.Response] | None = None):
        self.requests: list[httpx.Request] = []
        self._body = completion() if body is None else body
        self.mock = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if callable(self._body):
            return self._body(request)
        return httpx.Response(200, content=json.dumps(self._body))

    @property
    def clients(self) -> dict:
        return {
            "http_client": httpx.Client(transport=self.mock),
            "http_async_client": httpx.AsyncClient(transport=self.mock),
        }


@pytest.fixture
def live(monkeypatch):
    """Turn live mode on for one test. Safe: every live test passes a MockTransport."""
    monkeypatch.setenv("NEMOGATE_LIVE", "1")
    monkeypatch.setenv("NEBIUS_API_KEY", FAKE_KEY)


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def read_kind(path: Path, kind: str) -> list[dict]:
    return [line for line in read_log(path) if line.get("kind") == kind]


def sse(*events: dict) -> httpx.Response:
    """A streamed chat completion: one `data:` event per dict, then [DONE]."""
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


def chunk(content: str | None, finish: str | None = None, usage: dict | None = None) -> dict:
    """One chat.completion.chunk event; `usage` set means the trailing usage-only chunk."""
    event = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 0, "model": "m"}
    if usage is not None:
        return {**event, "choices": [], "usage": usage}
    delta = {} if content is None else {"role": "assistant", "content": content}
    return {**event, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
