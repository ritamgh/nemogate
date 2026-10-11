"""Shared plumbing: model access (`get_model`), environment, and the spend log and cap."""

from common.env import MissingEnvError, is_live, load_dotenv, require_env
from common.llm import ConfigError, get_model
from common.spend import SpendCapReached

__all__ = [
    "ConfigError",
    "MissingEnvError",
    "SpendCapReached",
    "get_model",
    "is_live",
    "load_dotenv",
    "require_env",
]
