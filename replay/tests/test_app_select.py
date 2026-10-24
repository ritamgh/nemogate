"""NEMOGATE_APP picks the app the runner uses; unset, it is the reference app, as before."""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from replay import app as replay_app
from replay import carriage, runner


class FakeTask:
    id, constraint, boundary, user_message = "X9", "C-001", "delegation:lead->helper", "Say hi."


def _install_fake_app(monkeypatch) -> object:
    module = ModuleType("fake_selected_app")
    module.APP = SimpleNamespace(
        repo_root="/repo", tasks=lambda: [FakeTask()], marker="the fake app"
    )
    monkeypatch.setitem(sys.modules, "fake_selected_app", module)
    return module.APP


def test_unset_means_the_reference_app(monkeypatch):
    # Breaks if the default drifts: with no NEMOGATE_APP every existing command must still run
    # `refapp.app.APP`, the one object `default_runner` used before the selector existed.
    from refapp.app import APP

    monkeypatch.delenv("NEMOGATE_APP", raising=False)
    assert replay_app.DEFAULT_APP == "refapp.app:APP"
    assert replay_app.load_app() is APP
    assert runner.default_runner().app is APP
    assert carriage.load_user_messages() == {t.id: t.user_message for t in APP.tasks()}


def test_blank_means_the_reference_app(monkeypatch):
    # Breaks if an exported-but-empty NEMOGATE_APP (a shell's `NEMOGATE_APP=`) is read as a
    # module path and crashes every command.
    from refapp.app import APP

    monkeypatch.setenv("NEMOGATE_APP", "  ")
    assert replay_app.load_app() is APP


def test_the_env_var_picks_another_app_for_runner_refusals_and_messages(monkeypatch):
    # Breaks if any of the three places that named the reference app (the runner, its up-front
    # refusal check, the carriage message lookup) ignores the selector.
    fake = _install_fake_app(monkeypatch)
    monkeypatch.setenv("NEMOGATE_APP", "fake_selected_app:APP")
    assert replay_app.load_app() is fake
    assert runner.default_runner().app is fake
    assert carriage.load_user_messages() == {"X9": "Say hi."}
    # Task X9 exists only in the fake app; the refusal check would raise "unknown task ids" if it
    # still looked at the reference app. Its ledger entry is the real file's, so C-001 resolves.
    assert runner.default_refusals(["X9"], [], []) == []


@pytest.mark.parametrize(
    ("spec", "why"),
    [
        ("no_colon_here", "module:attr"),
        ("fake_selected_app:Nope", "no attribute"),
        ("module_that_is_not_there_zz:APP", "cannot import"),
        (":APP", "module:attr"),
    ],
)
def test_a_bad_spec_is_a_value_error_naming_the_variable(monkeypatch, spec, why):
    # Breaks if a typo falls back to the reference app (a paid sweep on the wrong app) or leaks
    # a bare ImportError / AttributeError the CLI does not turn into a refusal.
    _install_fake_app(monkeypatch)
    monkeypatch.setenv("NEMOGATE_APP", spec)
    with pytest.raises(ValueError, match="NEMOGATE_APP") as err:
        replay_app.load_app()
    assert why in str(err.value)


def test_the_runner_cli_refuses_a_bad_app_with_exit_2(monkeypatch, capsys):
    # Breaks if `python -m replay.runner` crashes with a traceback (or starts) on a bad selector.
    monkeypatch.setattr(runner, "is_live", lambda: True)
    monkeypatch.setenv("NEMOGATE_APP", "no_colon_here")
    args = ["--tasks", "X9", "--arms", "natural", "--operators", "none", "--n", "1", "--yes"]
    assert runner.main(args) == 2
    assert "NEMOGATE_APP" in capsys.readouterr().err
