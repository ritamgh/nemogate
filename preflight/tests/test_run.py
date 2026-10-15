import json

import pytest

from common.tests.conftest import FAKE_KEY, Transport, write_config
from preflight.probes import Variant
from preflight.run import planned_calls, run_probes, variant_models
from preflight.tests.conftest import scripted_factory
from schemas.config import load_config
from schemas.enums import Role

SERVED = {"m-helper", "m-sut", "m-planner"}


def test_planned_calls_is_roles_times_six_probes_times_tries():
    # Breaks if the number the CLI shows before spending drifts from what run_probes makes.
    assert planned_calls(3, 3) == 54
    assert planned_calls(3, 1) == 18


def test_run_probes_makes_exactly_the_planned_calls_and_one_result_per_probe_and_role(tmp_path):
    # Breaks if run_probes makes a call the plan did not count (a surprise charge), skips a
    # probe, or reports a probe twice. A stub raises on a call beyond its script, and each stub
    # must have used all of it.
    config = load_config(write_config(tmp_path / "config.yaml"))
    factory, models = scripted_factory(2)
    results = run_probes(config, 2, factory, SERVED)
    assert sum(len(m.responses) for m in models) == planned_calls(3, 2) == 36
    assert all(m._next == len(m.responses) for m in models)
    assert sorted(
        (r.probe, r.role.value, r.response_format, r.thinking) for r in results
    ) == sorted(
        (probe, role, fmt, thinking)
        for role in ("helper", "planner", "sut")
        for probe, fmt, thinking in [
            ("escape", "json_schema", False),
            ("escape", "json_object", False),
            ("tool_call", "json_schema", False),
            ("thinking", "json_schema", True),
            ("escape", "json_schema", True),
            ("tool_call", "json_schema", True),
            ("model_ids", None, None),
        ]
    )
    assert all(r.passed for r in results)


def test_run_probes_records_the_super_json_object_failure(tmp_path):
    # Breaks if prose under json_object is not attributed to the role and format that produced it.
    config = load_config(write_config(tmp_path / "config.yaml"))
    factory, _ = scripted_factory(2, bad_json_object=[Role.sut])
    results = run_probes(config, 2, factory, SERVED)
    failed = [r for r in results if not r.passed]
    assert [(r.probe, r.role, r.response_format) for r in failed] == [
        ("escape", Role.sut, "json_object")
    ]
    assert failed[0].counts == {"valid": 0, "exact": 0, "errors": 0}


def test_variant_models_send_that_variants_format_and_thinking_switch(live, tmp_path):
    # Breaks if a probe variant is not what reaches the wire: the temp config's hash is stale
    # (get_model refuses it), or the thinking switch or response format is the config.yaml one.
    path = write_config(tmp_path / "config.yaml")  # json_schema, thinking off
    transport = Transport()
    with variant_models(path, **transport.clients) as build:
        model = build(Variant(Role.sut, "json_object", True))
        model.bind(response_format={"type": "json_object"}).invoke("reply in JSON")
    body = json.loads(transport.requests[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["chat_template_kwargs"]["enable_thinking"] is True
    assert FAKE_KEY not in transport.requests[0].content.decode()


def test_variant_models_leave_the_config_file_alone(live, tmp_path):
    # Breaks if building a variant rewrites config.yaml (a shared file this module never writes).
    path = write_config(tmp_path / "config.yaml")
    before = path.read_bytes()
    with variant_models(path, **Transport().clients) as build:
        build(Variant(Role.helper, "json_object", False))
    assert path.read_bytes() == before


def test_variant_models_refuse_a_variant_the_json_object_guard_would_not_allow(live, tmp_path):
    # Breaks if a model built for json_object could send json_schema: the guard in common.llm
    # must see the variant's format, not the file's.
    path = write_config(tmp_path / "config.yaml")
    with variant_models(path, **Transport().clients) as build:
        model = build(Variant(Role.sut, "json_object", False))
        with pytest.raises(ValueError, match="conflicts with the safe config"):
            model.bind(response_format={"type": "json_schema", "json_schema": {}}).invoke("x")
