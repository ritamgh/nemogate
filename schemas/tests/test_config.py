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
    assert config.budget.total_usd == 60
    assert config.budget.stop_at_fraction == 0.8
    assert config.models[Role.helper].temperature == 0
    assert config.models[Role.planner].temperature == 0
    assert config.models[Role.sut].temperature is None
    # Exact IDs from GET /v1/models (docs/FACTS.md §2); the casing differs per model on purpose.
    assert config.models[Role.helper].id == "nvidia/Nemotron-3_5-Lightning"
    assert config.models[Role.sut].id == "nvidia/nemotron-3-super-120b-a12b"
    assert config.models[Role.planner].id == "nvidia/Nemotron-3-Ultra-550b-a55b"
    assert config.models[Role.planner].output_usd_per_mtok == 3.0
    assert config.token_factory.base_url == "https://api.tokenfactory.nebius.com/v1"
    # The C0 safe config (docs/FACTS.md §6): Super fails json_object; thinking off for every role.
    assert config.safe_config.response_format == "json_schema"
    assert config.safe_config.thinking is False
    assert config.safe_config.hash == "8c2f708f9c34"


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


@pytest.mark.parametrize("field", ["input_usd_per_mtok", "output_usd_per_mtok", "temperature"])
@pytest.mark.parametrize("bad", [-0.5, float("inf"), float("nan")])
def test_negative_or_non_finite_price_and_temperature_are_rejected(field, bad):
    # Breaks if a negative or non-finite price slips in: the spend guard would then
    # compute a negative or infinite cost and under- or over-count the credit.
    data = _committed_config_data()
    data["models"]["helper"][field] = bad
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_context_window_is_rejected(bad):
    data = _committed_config_data()
    data["models"]["helper"]["context_window"] = bad
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_non_finite_total_budget_is_rejected(bad):
    # Breaks if an infinite budget disables the 80% stop.
    data = _committed_config_data()
    data["budget"]["total_usd"] = bad
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_zero_price_and_zero_temperature_are_accepted():
    # Guards against over-tightening: free models and greedy decoding are legitimate.
    data = _committed_config_data()
    data["models"]["helper"].update(
        input_usd_per_mtok=0, output_usd_per_mtok=0.0, temperature=0, context_window=1
    )
    helper = AppConfig.model_validate(data).models[Role.helper]
    assert (helper.input_usd_per_mtok, helper.temperature, helper.context_window) == (0, 0, 1)
