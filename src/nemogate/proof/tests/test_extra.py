"""A missing `proof` extra fails with the install command, at the point the extra is needed."""

import asyncio
import importlib
import sys

import pytest

from nemogate.proof import import_extra, runner

INSTALL = r"pip install nemogate\[proof\]"


def test_import_extra_names_the_install_command_for_a_missing_module(monkeypatch):
    # Breaks if import_extra lets the bare ModuleNotFoundError through: a core install would show
    # "No module named 'contree_sdk'" and not say which extra to install.
    monkeypatch.setitem(sys.modules, "contree_sdk", None)
    with pytest.raises(ImportError, match=INSTALL) as info:
        import_extra("contree_sdk")
    assert "contree_sdk is not installed" in str(info.value)


def test_import_extra_returns_an_installed_module():
    # Breaks if import_extra wraps or alters a module that is there.
    assert import_extra("json") is sys.modules["json"]


def test_the_default_client_asks_for_the_extra_when_the_sdk_is_missing(monkeypatch):
    # Breaks if the real runner reaches for contree_sdk without the install hint: a live run on a
    # core install would end in a bare "No module named 'contree_sdk'".
    monkeypatch.setitem(sys.modules, "contree_sdk", None)
    with pytest.raises(ImportError, match=INSTALL):
        runner._default_client()


def test_a_run_asks_for_the_extra_when_the_sqlite_checkpointer_is_missing(monkeypatch):
    # Breaks if Runner._invoke imports the sqlite checkpointer without the install hint. The
    # import is the first thing the method does, so nothing else needs to be built.
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite.aio", None)
    bare_runner = runner.Runner.__new__(runner.Runner)
    with pytest.raises(ImportError, match=INSTALL):
        asyncio.run(bare_runner._invoke(None, None, None, None, "thread", None, None))


def test_the_sandbox_module_asks_for_the_extra_when_the_sdk_is_missing(monkeypatch):
    # Breaks if nemogate.proof.sandbox imports the SDK directly: the sandbox is built from the
    # runner's default backend, the first place a run touches it.
    monkeypatch.delitem(sys.modules, "nemogate.proof.sandbox", raising=False)
    monkeypatch.setitem(sys.modules, "contree_sdk.langchain.sandbox", None)
    with pytest.raises(ImportError, match=INSTALL):
        importlib.import_module("nemogate.proof.sandbox")
