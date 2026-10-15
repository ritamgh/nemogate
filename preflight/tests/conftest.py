"""Shared fixtures for preflight tests. Nothing here reaches the network, a model or a real .env."""

import json
from collections.abc import Collection

import pytest
from langchain_core.messages import AIMessage

import common.env
from common.llm import StubChatModel
from preflight.probes import ESCAPE_SAMPLE, EXTRACTION_FIELDS, Variant
from schemas.enums import Role

FAKE_KEY = "fake-key-sentinel-7f3a91c2"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # Breaks if a test reads the developer's real .env or spend log, or runs with live mode on.
    monkeypatch.setattr(common.env, "REPO_ROOT", tmp_path / "no-repo-root")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NEMOGATE_SPEND_LOG", str(tmp_path / "spend.jsonl"))
    monkeypatch.delenv("NEMOGATE_LIVE", raising=False)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)


@pytest.fixture
def live(monkeypatch):
    """Live mode on for one test. Safe: every live test here uses a MockTransport or a stub."""
    monkeypatch.setenv("NEMOGATE_LIVE", "1")
    monkeypatch.setenv("NEBIUS_API_KEY", FAKE_KEY)


def scripted_factory(tries: int, *, bad_json_object: Collection[Role] = ()):
    """A model factory of stubs scripted for one full `run_probes` pass at `tries` per probe.

    Every reply is correct, except that a role in `bad_json_object` answers prose under
    json_object (docs/FACTS.md section 6: Super, 0/3). Returns `(factory, models)`; `models`
    lists the stubs built so a test can check each was called exactly as often as scripted.
    """
    models: list[StubChatModel] = []

    def build(variant: Variant) -> StubChatModel:
        escape = json.dumps({"text": ESCAPE_SAMPLE})
        if variant.response_format == "json_object" and variant.role in bad_json_object:
            escape = "Let me think about the string first."
        call = {"name": "get_weather", "args": {"city": "Paris"}, "id": "c", "type": "tool_call"}
        extraction = json.dumps(EXTRACTION_FIELDS)
        if variant.thinking:
            replies = [AIMessage(content=extraction)] * tries
        else:
            replies = [AIMessage(content=escape)] * tries
            if variant.response_format == "json_schema":
                replies += [AIMessage(content="", tool_calls=[call])] * tries
        model = StubChatModel(role=variant.role.value, responses=replies)
        models.append(model)
        return model

    return build, models
