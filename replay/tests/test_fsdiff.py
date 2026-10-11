import asyncio
import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest

from replay import fsdiff
from replay.fsdiff import SCRIPT, diff, snapshot
from schemas.run import FsChange

IMAGE_UUID = "11111111-2222-3333-4444-555555555555"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def build_tree(root):
    """A tree with every shape of name and entry the snapshot has to cope with."""
    files = {
        "app/core/main.py": b"print('hi')\n",
        "notes with space.txt": b"hello\n",
        "multi\nline.txt": b"newline name\n",
        "café/日本.txt": b"unicode\n",
        "empty.txt": b"",
        "__pycache__/x.pyc": b"\x00junk",
        "app/__pycache__/y.cpython-312.pyc": b"\x00junk",
        ".pytest_cache/y": b"cache",
        "z.pyc": b"\x00junk",
    }
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    os.symlink("notes with space.txt", root / "link_to_file")
    os.symlink("app/core", root / "link_to_dir")


EXPECTED = {
    "app/core/main.py": sha(b"print('hi')\n"),
    "notes with space.txt": sha(b"hello\n"),
    "multi\nline.txt": sha(b"newline name\n"),
    "café/日本.txt": sha(b"unicode\n"),
    "empty.txt": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "link_to_file": "symlink:notes with space.txt",
    "link_to_dir": "symlink:app/core",
}


def run_script(root):
    return subprocess.run(
        [sys.executable, "-c", SCRIPT, str(root)], capture_output=True, text=True, check=False
    )


def test_script_walks_tree_with_awkward_names_and_skips_caches(tmp_path):
    build_tree(tmp_path)
    proc = run_script(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == EXPECTED


def test_script_hashes_match_known_sha256_values(tmp_path):
    (tmp_path / "hello.txt").write_bytes(b"hello\n")
    proc = run_script(tmp_path)
    assert json.loads(proc.stdout) == {
        "hello.txt": "sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
    }


def test_script_output_is_ascii_so_the_sdk_utf8_decode_cannot_fail(tmp_path):
    build_tree(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tmp_path)], capture_output=True, check=True
    )
    proc.stdout.decode("ascii")


def test_script_fails_when_root_is_missing(tmp_path):
    proc = run_script(tmp_path / "nope")
    assert proc.returncode != 0


def test_script_ignores_fifos_instead_of_hanging(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"a")
    os.mkfifo(tmp_path / "pipe")
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tmp_path)], capture_output=True, text=True, timeout=20
    )
    assert json.loads(proc.stdout) == {"a.txt": sha(b"a")}


class FakeImage:
    def __init__(self, outcome, calls):
        self.outcome = outcome
        self.calls = calls

    def run(self, **kw):
        self.calls.append(("run", kw))

        async def go():
            return self.outcome

        return go()


class FakeClient:
    def __init__(self, exit_code=0, stdout="{}", truncated=False, stderr=""):
        self.calls = []
        outcome = SimpleNamespace(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            result=SimpleNamespace(truncated=truncated),
        )
        self.image = FakeImage(outcome, self.calls)
        self.images = SimpleNamespace(use=self._use)

    async def _use(self, ref):
        self.calls.append(("use", ref))
        return self.image


def test_snapshot_runs_the_real_script_output_through_a_disposable_run(tmp_path):
    build_tree(tmp_path)
    stdout = run_script(tmp_path).stdout
    client = FakeClient(stdout=stdout)

    result = asyncio.run(snapshot(client, IMAGE_UUID, "/repo", timeout=33))

    assert result == EXPECTED
    assert client.calls[0] == ("use", UUID(IMAGE_UUID))
    kind, kw = client.calls[1]
    assert kind == "run"
    assert kw["disposable"] is True
    assert kw["timeout"] == 33
    assert kw["shell"].startswith("python3 -c ")
    assert kw["shell"].endswith(" /repo")
    assert len(client.calls) == 2


def test_snapshot_shell_command_works_under_sh(tmp_path):
    root = tmp_path / "my repo"
    root.mkdir()
    build_tree(root)
    client = FakeClient()
    asyncio.run(snapshot(client, IMAGE_UUID, str(root)))
    proc = subprocess.run(
        ["sh", "-c", client.calls[1][1]["shell"]], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == EXPECTED


def test_snapshot_quotes_a_root_with_spaces():
    client = FakeClient()
    asyncio.run(snapshot(client, IMAGE_UUID, "/my repo"))
    assert client.calls[1][1]["shell"].endswith(" '/my repo'")


def test_snapshot_nonzero_exit_raises_with_stderr():
    client = FakeClient(exit_code=2, stderr="NotADirectoryError: /repo")
    with pytest.raises(RuntimeError, match="exit code 2.*NotADirectoryError"):
        asyncio.run(snapshot(client, IMAGE_UUID, "/repo"))


def test_snapshot_truncated_output_raises():
    client = FakeClient(stdout='{"a": "sha256:00"}', truncated=True)
    with pytest.raises(RuntimeError, match="truncated"):
        asyncio.run(snapshot(client, IMAGE_UUID, "/repo"))


@pytest.mark.parametrize("stdout", ["not json", "", "[1, 2]", '{"a": 1}'])
def test_snapshot_unparsable_or_wrongly_shaped_stdout_raises(stdout):
    client = FakeClient(stdout=stdout)
    with pytest.raises(RuntimeError, match="snapshot"):
        asyncio.run(snapshot(client, IMAGE_UUID, "/repo"))


def test_skip_constants_are_the_generic_python_caches():
    assert fsdiff.SKIP_DIRS == frozenset({"__pycache__", ".pytest_cache"})
    assert fsdiff.SKIP_SUFFIXES == frozenset({".pyc"})


def test_diff_added_deleted_modified_unchanged_sorted():
    base = {
        "same.py": "sha256:aa",
        "gone.py": "sha256:bb",
        "edit.py": "sha256:cc",
        "link": "symlink:old",
    }
    final = {
        "same.py": "sha256:aa",
        "edit.py": "sha256:dd",
        "new/b.py": "sha256:ee",
        "a.py": "sha256:ff",
        "link": "symlink:new",
    }
    out = diff(base, final)
    assert all(isinstance(c, FsChange) for c in out)
    assert [(c.path, c.op.value) for c in out] == [
        ("a.py", "added"),
        ("edit.py", "modified"),
        ("gone.py", "deleted"),
        ("link", "modified"),
        ("new/b.py", "added"),
    ]


def test_diff_identical_snapshots_is_empty():
    snap = {"a": "sha256:01", "b": "symlink:a"}
    assert diff(snap, dict(snap)) == []
    assert diff({}, {}) == []
