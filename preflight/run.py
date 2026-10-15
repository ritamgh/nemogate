"""Run the probes for every role in the config, building each model through `get_model`."""

import tempfile
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from langchain_core.language_models import BaseChatModel

from common.llm import get_model, safe_config_hash
from preflight.probes import (
    RESPONSE_FORMATS,
    ProbeResult,
    Variant,
    escape_probe,
    model_ids_probe,
    thinking_probe,
    tool_call_probe,
)
from schemas.config import AppConfig, SafeConfig, load_config

ModelFactory = Callable[[Variant], BaseChatModel]

# Model calls per role per try: escape under each response format and one tool call, all with
# thinking off; then with thinking on under json_schema: extraction, escape and a tool call.
CALLS_PER_ROLE_PER_TRY = len(RESPONSE_FORMATS) + 1 + 3


def planned_calls(n_roles: int, tries: int) -> int:
    """Model calls a run makes. The `/v1/models` listing is one more, free, request."""
    return n_roles * CALLS_PER_ROLE_PER_TRY * tries


@contextmanager
def variant_models(config_path: Path, **model_kwargs: Any) -> Iterator[ModelFactory]:
    """A factory of `get_model(role)` models, one per (response format, thinking) variant.

    `get_model` takes both settings from the config and refuses a stale hash, so each variant is
    a temporary copy of the config with that safe config and its own hash. The repo's
    `config.yaml` is only read. `model_kwargs` go to `get_model` (tests pass an httpx client).
    """
    base = load_config(config_path)
    with tempfile.TemporaryDirectory() as tmp:

        def build(variant: Variant) -> BaseChatModel:
            safe = SafeConfig(response_format=variant.response_format, thinking=variant.thinking)
            config = base.model_copy(update={"safe_config": safe})
            safe = safe.model_copy(update={"hash": safe_config_hash(config)})
            config = config.model_copy(update={"safe_config": safe})
            path = Path(tmp) / f"{variant.response_format}-{variant.thinking}.yaml"
            path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
            return get_model(variant.role, config_path=path, **model_kwargs)

        yield build


def run_probes(
    config: AppConfig,
    tries: int,
    factory: ModelFactory,
    served: Collection[str] | None,
    list_error: str | None = None,
) -> list[ProbeResult]:
    """Every probe for every role. A model is built once per variant and shared by its probes."""
    models: dict[Variant, BaseChatModel] = {}

    def model(variant: Variant) -> BaseChatModel:
        if variant not in models:
            models[variant] = factory(variant)
        return models[variant]

    results: list[ProbeResult] = []
    for role in config.models:
        for fmt in RESPONSE_FORMATS:
            variant = Variant(role, fmt, False)
            results.append(escape_probe(model(variant), variant, tries))
        default = Variant(role, "json_schema", False)
        results.append(tool_call_probe(model(default), default, tries))
        thinking_on = Variant(role, "json_schema", True)
        results.append(thinking_probe(model(thinking_on), thinking_on, tries))
        results.append(escape_probe(model(thinking_on), thinking_on, tries))
        results.append(tool_call_probe(model(thinking_on), thinking_on, tries))
        results.append(model_ids_probe(role, config.models[role].id, served, list_error))
    return results
