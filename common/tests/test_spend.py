import fcntl
import json
import multiprocessing
import threading
import time

import pytest

import common.env
from common import SpendCapReached
from common.spend import (
    admit,
    append_spend,
    check_cap,
    cost_usd,
    run_scope,
    run_totals,
    spend_log_path,
    total_spent,
)
from common.tests.conftest import make_repo_with_worktree, read_kind, read_log, write_config
from schemas.config import ModelConfig, load_config
from schemas.enums import Role


def test_cost_usd_uses_per_million_prices():
    # Breaks if the price unit (per Mtok) or the in/out pairing is wrong.
    cfg = ModelConfig(input_usd_per_mtok=1.0, output_usd_per_mtok=3.0)
    assert cost_usd(1000, 500, cfg) == pytest.approx(0.0025)
    assert cost_usd(2_000_000, 0, cfg) == pytest.approx(2.0)


SETTLE_1_5 = '{"kind": "settle", "call_id": "a", "role": "sut", "cost_usd": 1.5}\n'
SETTLE_0_25 = '{"kind": "settle", "call_id": "b", "role": "sut", "cost_usd": 0.25}\n'
SETTLE_9 = '{"kind": "settle", "call_id": "c", "role": "sut", "cost_usd": 9.0}\n'


def _default_log(monkeypatch, tmp_path):
    """Drop the test override so spend_log_path() resolves the real default (under fake HOME)."""
    monkeypatch.delenv("NEMOGATE_SPEND_LOG")
    return tmp_path / "home" / ".nemogate" / "spend.jsonl"


def _legacy_log(root, text):
    legacy = root / "runs" / "spend.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(text, encoding="utf-8")
    return legacy


def test_log_path_default_is_under_home_and_override_wins(monkeypatch, tmp_path):
    # Breaks if the default stays in the checkout's runs/ (a clean or fresh clone resets spend
    # to $0), or the env override stops winning.
    assert spend_log_path() == tmp_path / "spend.jsonl"
    assert _default_log(monkeypatch, tmp_path) == spend_log_path()


def test_first_write_to_the_default_log_creates_the_home_directory(monkeypatch, tmp_path):
    # Breaks if ~/.nemogate is not created on first write, so the very first live call crashes.
    default = _default_log(monkeypatch, tmp_path)
    assert not default.parent.exists()
    append_spend("sut", "m", 1, 1, 1.5)
    assert total_spent() == 1.5
    assert default.parent.is_dir()


def test_legacy_log_is_merged_into_the_home_log_and_left_untouched(monkeypatch, tmp_path):
    # Breaks if the legacy runs/spend.jsonl is ignored (money already spent stops counting),
    # moved, or edited.
    default = _default_log(monkeypatch, tmp_path)
    main, _ = make_repo_with_worktree(tmp_path)
    legacy = _legacy_log(main, SETTLE_1_5 + SETTLE_0_25)
    monkeypatch.setattr(common.env, "REPO_ROOT", main)
    assert total_spent() == 1.75
    assert default.read_text(encoding="utf-8") == SETTLE_1_5 + SETTLE_0_25
    assert [p.name for p in default.parent.iterdir()] == ["spend.jsonl"]  # no temp file left
    assert legacy.read_text(encoding="utf-8") == SETTLE_1_5 + SETTLE_0_25


def test_a_home_log_created_first_does_not_hide_the_legacy_log(monkeypatch, tmp_path):
    # Breaks if the legacy log is merged only when the home log is absent: a fresh clone's
    # home log would hide the money an older checkout already spent.
    default = _default_log(monkeypatch, tmp_path)
    default.parent.mkdir(parents=True)
    default.write_text(SETTLE_0_25, encoding="utf-8")
    main, _ = make_repo_with_worktree(tmp_path)
    legacy = _legacy_log(main, SETTLE_9)
    monkeypatch.setattr(common.env, "REPO_ROOT", main)
    assert total_spent() == 9.25
    assert default.read_text(encoding="utf-8") == SETTLE_0_25 + SETTLE_9
    assert legacy.read_text(encoding="utf-8") == SETTLE_9


def test_lines_appended_to_the_legacy_log_after_the_merge_still_count(monkeypatch, tmp_path):
    # Breaks if the merge is one-shot: an older process still writing the legacy log after the
    # first copy would have its spend lost.
    default = _default_log(monkeypatch, tmp_path)
    main, _ = make_repo_with_worktree(tmp_path)
    legacy = _legacy_log(main, SETTLE_1_5)
    monkeypatch.setattr(common.env, "REPO_ROOT", main)
    assert total_spent() == 1.5
    append_spend("sut", "m", 1, 1, 0.5)
    legacy.write_text(legacy.read_text(encoding="utf-8") + SETTLE_9, encoding="utf-8")
    assert total_spent() == 11.0
    assert SETTLE_9 in default.read_text(encoding="utf-8")
    assert legacy.read_text(encoding="utf-8") == SETTLE_1_5 + SETTLE_9


def test_merging_the_legacy_log_twice_adds_nothing(monkeypatch, tmp_path):
    # Breaks if a legacy line already in the home log is appended again (double counting).
    default = _default_log(monkeypatch, tmp_path)
    main, _ = make_repo_with_worktree(tmp_path)
    _legacy_log(main, SETTLE_1_5 + SETTLE_0_25)
    monkeypatch.setattr(common.env, "REPO_ROOT", main)
    assert total_spent() == 1.75
    first = default.read_text(encoding="utf-8")
    assert total_spent() == 1.75
    assert default.read_text(encoding="utf-8") == first == SETTLE_1_5 + SETTLE_0_25


def test_a_worktree_carries_over_the_main_checkouts_legacy_log(monkeypatch, tmp_path):
    # Breaks if the legacy path is read from the current worktree instead of the main checkout.
    _default_log(monkeypatch, tmp_path)
    main, wt = make_repo_with_worktree(tmp_path)
    _legacy_log(main, SETTLE_1_5)
    monkeypatch.setattr(common.env, "REPO_ROOT", wt)
    assert total_spent() == 1.5


def test_without_a_legacy_log_nothing_is_created_by_reading(monkeypatch, tmp_path):
    # Breaks if a read creates an empty log, or a missing legacy file crashes the carry-over.
    default = _default_log(monkeypatch, tmp_path)
    assert total_spent() == 0.0
    assert not default.exists()


def test_the_override_ignores_the_legacy_log(monkeypatch, tmp_path):
    # Breaks if the carry-over also runs for NEMOGATE_SPEND_LOG, leaking real spend into tests.
    main, _ = make_repo_with_worktree(tmp_path)
    _legacy_log(main, SETTLE_9)
    monkeypatch.setattr(common.env, "REPO_ROOT", main)
    assert total_spent() == 0.0
    assert not (tmp_path / "home" / ".nemogate").exists()


def test_append_creates_parent_dir_and_writes_documented_fields(monkeypatch, tmp_path):
    # Breaks if the log line drops a field, or a missing runs/ dir crashes the first call.
    target = tmp_path / "deep" / "dir" / "spend.jsonl"
    monkeypatch.setenv("NEMOGATE_SPEND_LOG", str(target))
    append_spend(Role.sut, "m-sut", 1000, 500, 0.0025)
    (line,) = read_log(target)
    assert set(line) == {
        "kind",
        "call_id",
        "ts",
        "role",
        "model_id",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "run_id",
    }
    assert line["kind"] == "settle"
    assert line["call_id"]
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


def test_an_unsettled_reserve_counts_and_a_settle_replaces_its_reserve(spend_log, config_path):
    # Breaks if a failed call drops out of the total (reserve ignored), or a settled call is
    # counted twice (reserve + settle) instead of at its actual cost.
    config = load_config(config_path)
    model_cfg = config.models[Role.sut]
    admit(config, Role.sut, "m-sut", "call-a", 0, 1_000_000, model_cfg)  # reserves 3.0
    assert total_spent() == pytest.approx(3.0)
    admit(config, Role.sut, "m-sut", "call-b", 1_000_000, 0, model_cfg)  # reserves 1.0
    assert total_spent() == pytest.approx(4.0)
    append_spend("sut", "m-sut", 100_000, 0, 0.1, call_id="call-a")  # settles call-a at 0.1
    assert total_spent() == pytest.approx(1.1)


def test_admit_writes_the_documented_reserve_line(spend_log, tmp_path):
    # Breaks if the reserve drops a field or prices the estimate wrongly (2M in * 1.0 + 1M out
    # * 3.0 per Mtok = 5.0 for sut). Uses a roomier cap so the 5.0 fits.
    config = load_config(write_config(tmp_path / "roomy.yaml", budget__total_usd=100))
    with run_scope("r-9"):
        admit(config, Role.sut, "m-sut", "call-a", 2_000_000, 1_000_000, config.models[Role.sut])
    (line,) = read_log(spend_log)
    assert set(line) == {
        "kind",
        "call_id",
        "ts",
        "role",
        "model_id",
        "est_input_tokens",
        "est_output_tokens",
        "cost_usd",
        "run_id",
    }
    assert line["kind"] == "reserve"
    assert line["call_id"] == "call-a"
    assert (line["role"], line["model_id"]) == ("sut", "m-sut")
    assert (line["est_input_tokens"], line["est_output_tokens"]) == (2_000_000, 1_000_000)
    assert line["cost_usd"] == pytest.approx(5.0)
    assert line["run_id"] == "r-9"


def test_admit_refuses_when_spent_plus_estimate_exceeds_the_cap(spend_log, config_path):
    # Breaks if admit compares spent alone to the cap (cap 8.0: 6.0 + 3.0 > 8.0 is refused,
    # 5.0 + 3.0 == 8.0 is allowed), or writes a reserve for a refused call.
    config = load_config(config_path)
    model_cfg = config.models[Role.sut]
    append_spend("sut", "m", 1, 1, 6.0)
    with pytest.raises(SpendCapReached) as exc:
        admit(config, Role.sut, "m-sut", "call-a", 0, 1_000_000, model_cfg)
    assert "stop and tell a human (AGENTS.md rule 3)" in str(exc.value)
    assert len(read_log(spend_log)) == 1
    spend_log.write_text("")
    append_spend("sut", "m", 1, 1, 5.0)
    admit(config, Role.sut, "m-sut", "call-a", 0, 1_000_000, model_cfg)
    assert len(read_kind(spend_log, "reserve")) == 1


def _admit_once(config_path: str, barrier, results) -> None:
    config = load_config(config_path)
    barrier.wait(timeout=60)
    try:
        admit(
            config,
            Role.sut,
            "m-sut",
            f"call-{multiprocessing.current_process().name}",
            0,
            1_000_000,  # reserves 3.0
            config.models[Role.sut],
        )
        results.put("ok")
    except SpendCapReached:
        results.put("refused")


def test_two_processes_cannot_both_be_admitted_into_the_last_room(spend_log, config_path):
    # Breaks if the cap check and the reserve write are not one atomic step under the lock:
    # cap 8.0 with 4.0 spent leaves room for one 3.0 reserve, not two.
    append_spend("sut", "m", 1, 1, 4.0)
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    workers = [
        ctx.Process(target=_admit_once, args=(str(config_path), barrier, results)) for _ in range(2)
    ]
    for p in workers:
        p.start()
    outcomes = sorted(results.get(timeout=120) for _ in workers)
    for p in workers:
        p.join(timeout=60)
        assert p.exitcode == 0
    assert outcomes == ["ok", "refused"]
    assert len(read_kind(spend_log, "reserve")) == 1
    assert total_spent() == pytest.approx(7.0)


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


def test_run_totals_take_tokens_from_settles_and_cost_includes_unsettled_reserves(
    spend_log, config_path
):
    # Breaks if reserve estimates leak into the token counts, or an unsettled call is left out
    # of the run's cost.
    config = load_config(config_path)
    with run_scope("r-1"):
        admit(config, Role.sut, "m-sut", "settled", 0, 1_000_000, config.models[Role.sut])  # 3.0
        append_spend("sut", "m-sut", 100, 50, 0.5, call_id="settled")
        admit(config, Role.helper, "m-helper", "lost", 0, 1_000_000, config.models[Role.helper])
    with run_scope("r-2"):
        append_spend("planner", "m", 7, 7, 0.25)
    tokens, cost = run_totals("r-1")
    assert tokens == {Role.helper: 0, Role.sut: 150, Role.planner: 0}
    assert cost == pytest.approx(2.5)  # 0.5 settled + 2.0 unsettled helper reserve (1M out * 2.0)


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
