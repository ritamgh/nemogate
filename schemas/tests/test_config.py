from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from schemas.config import AppConfig, load_config
from schemas.enums import Role

REPO_ROOT = Path(__file__).resolve().parents[2]


def _committed_config_data():
    return yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))


def test_committed_config_yaml_loads():
    # Breaks if config.yaml and AppConfig drift apart (a typo'd key, a wrong type).
    config = load_config(REPO_ROOT / "config.yaml")
    assert set(config.models) == {Role.helper, Role.sut, Role.planner}
    assert config.budget.total_usd == 30
    assert config.budget.stop_at_fraction == 0.8
    assert config.models[Role.helper].temperature == 0
    assert config.models[Role.planner].temperature == 0
    assert config.models[Role.sut].temperature is None
    assert config.models[Role.sut].id is None
    assert config.token_factory.base_url is None
    assert config.safe_config.response_format is None


def test_config_with_unknown_top_level_key_is_rejected():
    # Breaks if extra="forbid" is lost, which would hide a misspelled section name.
    with pytest.raises(ValidationError):
        AppConfig.model_validate({**_committed_config_data(), "budgt": {"total_usd": 1}})


def test_config_missing_a_model_role_is_rejected():
    data = _committed_config_data()
    del data["models"]["planner"]
    with pytest.raises(ValidationError, match="planner"):
        AppConfig.model_validate(data)


def test_stop_at_fraction_above_one_is_rejected():
    data = _committed_config_data()
    data["budget"]["stop_at_fraction"] = 1.5
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_non_positive_total_budget_is_rejected():
    data = _committed_config_data()
    data["budget"]["total_usd"] = 0
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_unknown_response_format_is_rejected():
    data = _committed_config_data()
    data["safe_config"]["response_format"] = "free_text"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)
