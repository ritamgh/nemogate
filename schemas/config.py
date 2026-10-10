"""The repo-root `config.yaml`: model ids, prices, temperatures, safe config and budget."""

from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, Field, model_validator

from schemas.enums import STRICT, Role

NonNegativeFinite = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class TokenFactoryConfig(BaseModel):
    model_config = STRICT
    base_url: str | None = None


class ModelConfig(BaseModel):
    model_config = STRICT
    id: str | None = None
    input_usd_per_mtok: NonNegativeFinite | None = None
    output_usd_per_mtok: NonNegativeFinite | None = None
    temperature: NonNegativeFinite | None = None
    context_window: Annotated[int, Field(gt=0)] | None = None


class SafeConfig(BaseModel):
    model_config = STRICT
    response_format: Literal["json_schema", "json_object"] | None = None
    thinking: bool | None = None
    hash: str | None = None


class BudgetConfig(BaseModel):
    model_config = STRICT
    total_usd: float = Field(gt=0, allow_inf_nan=False)
    stop_at_fraction: float = Field(gt=0, le=1)


class AppConfig(BaseModel):
    model_config = STRICT
    token_factory: TokenFactoryConfig
    models: dict[Role, ModelConfig]
    safe_config: SafeConfig
    budget: BudgetConfig

    @model_validator(mode="after")
    def _models_cover_every_role(self) -> Self:
        missing = sorted(role.value for role in Role if role not in self.models)
        if missing:
            raise ValueError(f"models is missing roles: {', '.join(missing)}")
        return self


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate the app config. A relative path resolves against the working directory."""
    return AppConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
