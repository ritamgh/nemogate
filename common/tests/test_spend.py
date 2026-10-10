import fcntl
import json
import multiprocessing
import threading
import time

import pytest

from common import SpendCapReached
from common.spend import (
    append_spend,
    check_cap,
    cost_usd,
    run_scope,
    run_totals,
    spend_log_path,
    total_spent,
)
from common.tests.conftest import read_log
from schemas.config import ModelConfig, load_config
from schemas.enums import Role


def test_cost_usd_uses_per_million_prices():
    # Breaks if the price unit (per Mtok) or the in/out pairing is wrong.
    cfg = ModelConfig(input_usd_per_mtok=1.0, output_usd_per_mtok=3.0)
    assert cost_usd(1000, 500, cfg) == pytest.approx(0.0025)
    assert cost_usd(2_000_000, 0, cfg) == pytest.approx(2.0)


def test_log_path_default_and_override(monkeypatch, tmp_path):
    # Breaks if the env override is ignored or the default leaves runs/spend.jsonl.
    assert spend_log_path() == tmp_path / "spend.jsonl"
    monkeypatch.delenv("NEMOGATE_SPEND_LOG")
    assert spend_log_path().parts[-2:] == ("runs", "spend.jsonl")


def test_append_creates_parent_dir_and_writes_documented_fields(monkeypatch, tmp_path):
    # Breaks if the log line drops a field, or a missing runs/ dir crashes the first call.
    target = tmp_path / "deep" / "dir" / "spend.jsonl"
    monkeypatch.setenv("NEMOGATE_SPEND_LOG", str(target))
    append_spend(Role.sut, "m-sut", 1000, 500, 0.0025)
    (line,) = read_log(target)
    assert set(line) == {
        "ts",
        "role",
        "model_id",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "run_id",
    }
    assert line["ts"].endswith("+00:00")
    assert line["role"] == "sut"
    assert line["model_id"] == "m-sut"
    assert (line["input_tokens"], line["output_tokens"]) == (1000, 500)
    assert line["cost_usd"] == 0.0025
    assert line["run_id"] is None


def test_total_spent_sums_and_is_zero_without_a_log(spend_log):
    # Breaks if a missing log raises, or the sum skips lines.
    assert total_spent() == 0.0
    append_spend("sut", "m", 1, 1, 1.5)
    append_spend("helper", "m", 1, 1, 0.25)
    assert total_spent() == pytest.approx(1.75)


def test_run_scope_tags_lines_and_run_totals_cover_every_role(spend_log):
    # Breaks if run_id is not picked up, other runs leak into the sum, or a role key is missing.
    with run_scope("r-1"):
        append_spend("sut", "m", 100, 50, 0.5)
        append_spend("sut", "m", 10, 5, 0.25)
        append_spend("helper", "m", 7, 3, 0.125)
    with run_scope("r-2"):
        append_spend("planner", "m", 1000, 1000, 9.0)
    append_spend("sut", "m", 1, 1, 0.0625)  # outside any scope
    lines = read_log(spend_log)
    assert [line["run_id"] for line in lines] == ["r-1", "r-1", "r-1", "r-2", None]
    tokens, cost = run_totals("r-1")
    assert tokens == {Role.helper: 10, Role.sut: 165, Role.planner: 0}
    assert cost == pytest.approx(0.875)
    assert run_totals("never-ran") == ({Role.helper: 0, Role.sut: 0, Role.planner: 0}, 0.0)


def test_run_scope_restores_the_previous_value(spend_log):
    # Breaks if leaving a scope leaves its run_id behind for later calls.
    with run_scope("outer"):
        with run_scope("inner"):
            append_spend("sut", "m", 1, 1, 0.1)
        append_spend("sut", "m", 1, 1, 0.1)
    append_spend("sut", "m", 1, 1, 0.1)
    assert [line["run_id"] for line in read_log(spend_log)] == ["inner", "outer", None]


def _append_many(path: str, worker: int, count: int) -> None:
    pad = "x" * 20000  # larger than one buffered write, so only the lock keeps lines whole
    for i in range(count):
        append_spend("sut", f"{pad}-{worker}-{i}", 1, 1, 0.001, log_path=path)


def test_parallel_processes_do_not_interleave_or_lose_lines(spend_log):
    # Breaks if the append loses its flock: big lines from 4 processes would interleave.
    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_append_many, args=(str(spend_log), w, 25)) for w in range(4)]
    for p in workers:
        p.start()
    for p in workers:
        p.join(timeout=120)
        assert p.exitcode == 0
    lines = [json.loads(line) for line in spend_log.read_text().splitlines()]
    assert len(lines) == 100
    assert len({line["model_id"] for line in lines}) == 100
    assert total_spent() == pytest.approx(0.1)


def test_append_and_read_wait_for_the_file_lock(spend_log):
    # Breaks if append or read skips flock: they would run while another process holds the log.
    append_spend("sut", "m", 1, 1, 0.5)
    holder = spend_log.open("a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    append_thread = threading.Thread(target=lambda: append_spend("sut", "m", 1, 1, 0.5))
    read_thread = threading.Thread(target=total_spent)
    append_thread.start()
    read_thread.start()
    time.sleep(0.5)
    assert append_thread.is_alive()
    assert read_thread.is_alive()
    assert len(read_log(spend_log)) == 1
    fcntl.flock(holder, fcntl.LOCK_UN)
    holder.close()
    append_thread.join(timeout=10)
    read_thread.join(timeout=10)
    assert not append_thread.is_alive()
    assert not read_thread.is_alive()
    assert len(read_log(spend_log)) == 2


def test_check_cap_blocks_at_the_cap_and_allows_just_below(spend_log, config_path):
    # Breaks if the cap uses > instead of >=, or ignores stop_at_fraction (10 * 0.8 = 8.0).
    config = load_config(config_path)
    append_spend("sut", "m", 1, 1, 7.999)
    check_cap(config)
    append_spend("sut", "m", 1, 1, 0.001)
    with pytest.raises(SpendCapReached) as exc:
        check_cap(config)
    text = str(exc.value)
    assert "8.0" in text
    assert "stop and tell a human (AGENTS.md rule 3)" in text
    assert isinstance(exc.value, RuntimeError)
