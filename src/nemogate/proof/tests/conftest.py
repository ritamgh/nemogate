"""Shared fixtures for replay tests. Nothing here reaches the network, Contree or a model."""

import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # Breaks if a test writes the developer's real spend log, reads their Contree credentials or
    # turns on live mode: HOME and CONTREE_HOME point into tmp_path, the spend log is a tmp file
    # and NEMOGATE_LIVE is unset.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CONTREE_HOME", str(tmp_path / "contree-home"))
    monkeypatch.setenv("NEMOGATE_SPEND_LOG", str(tmp_path / "spend.jsonl"))
    monkeypatch.delenv("NEMOGATE_LIVE", raising=False)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
