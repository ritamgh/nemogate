import json

import pytest

from common.tests.conftest import write_config
from preflight.__main__ import main
from preflight.tests.conftest import scripted_factory
from schemas.enums import Role

SERVED = ["m-helper", "m-sut", "m-planner"]


def _forbidden(*args, **kwargs):
    raise AssertionError("a model or a request was made")


def test_without_the_live_flag_it_prints_the_call_count_and_runs_nothing(tmp_path, capsys):
    # Breaks if the preflight can spend credit without NEMOGATE_LIVE=1, or does not say how
    # many calls it would make (3 roles x 4 per try x 3 tries = 36).
    config = write_config(tmp_path / "config.yaml")
    out = tmp_path / "proposal.json"
    rc = main(
        ["--out", str(out), "--yes", "--config", str(config)],
        model_factory=_forbidden,
        list_ids=_forbidden,
    )
    assert rc == 1
    text = capsys.readouterr().out
    assert "planned: 36 model calls (3 roles x 4 per try x 3 tries)" in text
    assert "NEMOGATE_LIVE is not 1: nothing was run" in text
    assert not out.exists()


def test_live_without_yes_still_runs_nothing(live, tmp_path, capsys):
    # Breaks if the opt-in alone starts the calls: the count must be confirmed with --yes.
    config = write_config(tmp_path / "config.yaml")
    out = tmp_path / "proposal.json"
    rc = main(
        ["--out", str(out), "--tries", "2", "--config", str(config)],
        model_factory=_forbidden,
        list_ids=_forbidden,
    )
    assert rc == 1
    text = capsys.readouterr().out
    assert "planned: 24 model calls" in text
    assert "Re-run with --yes" in text
    assert not out.exists()


def test_out_may_not_be_the_config_file(live, tmp_path):
    # Breaks if the proposal can overwrite config.yaml, the shared file only B's approval changes.
    config = write_config(tmp_path / "config.yaml")
    before = config.read_bytes()
    with pytest.raises(SystemExit) as exit_info:
        main(["--out", str(config), "--yes", "--config", str(config)], model_factory=_forbidden)
    assert exit_info.value.code == 2
    assert config.read_bytes() == before


def test_a_full_run_writes_the_proposal_and_leaves_config_alone(live, tmp_path, capsys):
    # The FACTS case end to end: Super answers prose under json_object (0/N), everything else is
    # fine. Breaks if the proposal is not json_schema/thinking off, if its hash is not the
    # config's stored one (0602bb99cc09, hand-computed in common/tests/conftest.py), if the
    # config file changes, or if a clean run does not exit 0.
    config = write_config(tmp_path / "config.yaml")
    before = config.read_bytes()
    out = tmp_path / "out" / "proposal.json"
    factory, models = scripted_factory(2, bad_json_object=[Role.sut])
    rc = main(
        ["--out", str(out), "--tries", "2", "--yes", "--config", str(config)],
        model_factory=factory,
        list_ids=lambda base_url: SERVED,
    )
    assert rc == 0
    assert config.read_bytes() == before
    document = json.loads(out.read_text())
    assert document["proposal"]["safe_config"] == {
        "response_format": "json_schema",
        "thinking": False,
        "hash": "0602bb99cc09",
    }
    assert document["proposal"]["differs_from_config"] == []
    assert document["proposal"]["warnings"] == []
    assert document["proposal"]["per_role"]["sut"]["json_object_thinking_off"] is False
    assert document["model_ids"] == {"helper": "m-helper", "sut": "m-sut", "planner": "m-planner"}
    assert document["served_models_listed"] is True
    assert len(document["results"]) == 15
    text = capsys.readouterr().out
    assert "FAIL escape     sut      json_object/thinking=False  0/2" in text
    assert "config.yaml already matches the proposal" in text
    assert sum(len(m.responses) for m in models) == 24


def test_a_failed_model_listing_is_reported_and_fails_the_run(live, tmp_path, capsys):
    # Breaks if an unreachable /v1/models is treated as "ids confirmed", or aborts the run
    # before the other probes' findings are written.
    config = write_config(tmp_path / "config.yaml")
    out = tmp_path / "proposal.json"
    factory, _ = scripted_factory(1)

    def failing(base_url):
        raise RuntimeError("GET https://tf.example.invalid/v1/models answered 500")

    rc = main(
        ["--out", str(out), "--tries", "1", "--yes", "--config", str(config)],
        model_factory=factory,
        list_ids=failing,
    )
    assert rc == 1
    document = json.loads(out.read_text())
    assert document["served_models_listed"] is False
    assert document["proposal"]["safe_config"]["response_format"] == "json_schema"
    assert sorted(document["proposal"]["warnings"]) == [
        "helper: the configured model id is not confirmed as served",
        "planner: the configured model id is not confirmed as served",
        "sut: the configured model id is not confirmed as served",
    ]
    ids = [r for r in document["results"] if r["probe"] == "model_ids"]
    assert ids[0]["evidence"] == [
        "could not list models: GET https://tf.example.invalid/v1/models answered 500"
    ]
    assert "WARNING: sut: the configured model id" in capsys.readouterr().out
