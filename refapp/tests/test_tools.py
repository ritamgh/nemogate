"""run_tests, request_approval and http_get, driven through real LocalShellBackend commands."""

import asyncio
import socket
import sys
from pathlib import Path
from shlex import quote

import pytest
from deepagents.backends import LocalShellBackend

from refapp.tools import make_tools

PY = quote(sys.executable)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def backend(tmp_path: Path) -> LocalShellBackend:
    return LocalShellBackend(root_dir=tmp_path, virtual_mode=True, inherit_env=True)


def test_run_tests_reports_exit_code_and_output_of_a_failing_run(backend, repo):
    # Breaks if run_tests drops the exit code or the command output.
    cmd = f"{PY} -c \"import sys; print('1 failed, 2 passed'); sys.exit(1)\""
    tools = make_tools(backend, shell_repo=str(repo), test_cmd=cmd)

    result = tools["run_tests"].invoke({})

    assert result.splitlines()[0] == "exit code: 1"
    assert "1 failed, 2 passed" in result


def test_run_tests_runs_in_shell_repo_and_passes_each_path_as_one_argument(backend, repo):
    # Breaks if the cd into shell_repo is lost or a path with a space is split in two.
    cmd = f'{PY} -c "import os, sys; print(os.getcwd(), sys.argv[1:])"'
    tools = make_tools(backend, shell_repo=str(repo), test_cmd=cmd)

    result = tools["run_tests"].invoke({"paths": ["tests/test_a.py", "tests/b c.py"]})

    assert result == f"exit code: 0\n{repo} ['tests/test_a.py', 'tests/b c.py']\n"


def test_run_tests_keeps_only_the_tail_of_long_output(backend, repo):
    # Breaks if the cap keeps the head (the summary line sits at the end) or no cap applies.
    cmd = f"{PY} -c \"print('x' * 10000); print('SUMMARY 5 passed')\""
    tools = make_tools(backend, shell_repo=str(repo), test_cmd=cmd)

    result = tools["run_tests"].invoke({})

    assert result.endswith("SUMMARY 5 passed\n")
    assert len(result) < 4200
    assert "truncated" in result.splitlines()[1]


def test_run_tests_works_under_ainvoke(backend, repo):
    # Breaks if the tool has no coroutine (ainvoke would fall back wrongly or fail).
    cmd = f"{PY} -c \"import sys; print('async ran'); sys.exit(2)\""
    tools = make_tools(backend, shell_repo=str(repo), test_cmd=cmd)

    result = asyncio.run(tools["run_tests"].ainvoke({}))

    assert result == "exit code: 2\nasync ran\n\nExit code: 2"


def test_request_approval_answers_approved_under_invoke_and_ainvoke(backend, repo):
    tools = make_tools(backend, shell_repo=str(repo))
    args = {"action": "delete the cache", "reason": "it is stale"}

    assert tools["request_approval"].invoke(args) == "Approved: delete the cache"
    assert asyncio.run(tools["request_approval"].ainvoke(args)) == "Approved: delete the cache"


def test_http_get_returns_the_same_canned_page_without_touching_the_network(
    backend, repo, monkeypatch
):
    # Breaks if http_get resolves a host or connects, or if the page depends on the url.
    # (socket.socket itself stays: asyncio's event loop needs a socketpair.)
    def no_network(*args, **kwargs):
        raise AssertionError("http_get touched the network")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    tools = make_tools(backend, shell_repo=str(repo))

    first = tools["http_get"].invoke({"url": "https://example.test/a"})
    second = asyncio.run(tools["http_get"].ainvoke({"url": "https://example.test/b"}))

    assert first == second
    assert first.startswith("<html>")
    assert "example.test" not in first


def test_make_tools_returns_exactly_the_three_named_tools(backend, repo):
    tools = make_tools(backend, shell_repo=str(repo))

    assert sorted(tools) == ["http_get", "request_approval", "run_tests"]
    assert all(name == tool.name for name, tool in tools.items())
    assert sorted(tools["run_tests"].args) == ["paths"]
    assert sorted(tools["request_approval"].args) == ["action", "reason"]
    assert sorted(tools["http_get"].args) == ["url"]


def test_run_tests_leaves_no_cache_files_in_the_repo(backend, repo):
    # Breaks if the default test command lets Python write __pycache__ or pytest write
    # .pytest_cache: the run's filesystem diff would then show files the agents never wrote
    # (a cache under a protected folder would read as a rule break).
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "mod.py").write_text("def one():\n    return 1\n")
    (repo / "test_mod.py").write_text(
        "from pkg.mod import one\n\n\ndef test_one():\n    assert one() == 1\n"
    )
    tools = make_tools(backend, shell_repo=str(repo))

    result = tools["run_tests"].invoke({})

    assert result.splitlines()[0] == "exit code: 0"
    leftovers = sorted(
        str(p.relative_to(repo))
        for p in repo.rglob("*")
        if p.name in ("__pycache__", ".pytest_cache")
    )
    assert leftovers == []
