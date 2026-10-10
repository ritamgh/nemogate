import asyncio
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from contree_sdk.sdk.exceptions import OperationTimedOutError

from refapp import sandbox, seedenv

FINAL_UUID = "11111111-2222-3333-4444-555555555555"
START_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeRun:
    """What `session.run(...)` returns: awaiting it gives the result."""

    def __init__(self, result):
        self.result = result

    def __await__(self):
        async def get():
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        return get().__await__()


class FakeBackend:
    """Records SDK calls in order; `script` maps a substring of a shell command to its result."""

    def __init__(self, script=None):
        self.calls: list[tuple] = []
        self.script = script or {}
        self.images = SimpleNamespace(oci=self._oci, use=self._use)

    async def _oci(self, ref):
        self.calls.append(("oci", ref))
        return FakeImage(self, START_UUID)

    async def _use(self, uuid):
        self.calls.append(("use", uuid))
        return FakeImage(self, str(uuid))

    def result_for(self, shell):
        for needle, result in self.script.items():
            if needle in shell:
                return result
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


class FakeImage:
    def __init__(self, backend, uuid):
        self.backend, self.uuid = backend, uuid

    def session(self):
        return FakeSession(self.backend, self.uuid)

    def run(self, **kw):
        self.backend.calls.append(("image.run", kw))
        return FakeRun(self.backend.result_for(kw["shell"]))


class FakeSession:
    def __init__(self, backend, uuid):
        self.backend, self.uuid, self.client = backend, uuid, backend

    async def apply_files(self, files):
        self.backend.calls.append(("apply_files", files))

    def run(self, **kw):
        self.backend.calls.append(("session.run", kw))
        result = self.backend.result_for(kw["shell"])
        if result.exit_code == 0 and "pytest" in kw["shell"]:
            self.uuid = FINAL_UUID
        return FakeRun(result)


def res(code, out="", err=""):
    return SimpleNamespace(exit_code=code, stdout=out, stderr=err)


def copy_seed(tmp_path, monkeypatch) -> Path:
    dest = tmp_path / "seed"
    shutil.copytree(seedenv.SEED_DIR, dest)
    monkeypatch.setattr(seedenv, "SEED_DIR", dest)
    return dest


def test_seed_files_has_the_app_and_migrations_but_no_caches(tmp_path, monkeypatch):
    # Fails if caches leak into the image (and the sha would then depend on local pytest runs).
    seed = copy_seed(tmp_path, monkeypatch)
    (seed / "app" / "__pycache__").mkdir()
    (seed / "app" / "__pycache__" / "api.cpython-312.pyc").write_bytes(b"x")
    (seed / "app" / "stray.pyc").write_bytes(b"x")
    (seed / ".pytest_cache").mkdir()
    (seed / ".pytest_cache" / "CACHEDIR.TAG").write_bytes(b"x")
    (seed / "tests" / ".pytest_cache").mkdir()
    (seed / "tests" / ".pytest_cache" / "v").write_bytes(b"x")

    files = sandbox.seed_files()

    assert "app/api.py" in files
    assert b"descripton" in files["migrations/0001_init.py"]
    assert files["requirements.txt"] == (seed / "requirements.txt").read_bytes()
    assert not [
        p for p in files if "__pycache__" in p or ".pytest_cache" in p or p.endswith(".pyc")
    ]


def test_seed_sha_is_12_hex_stable_and_tracks_one_byte(tmp_path, monkeypatch):
    # Fails if the sha ignores content (a stale cached image would be reused after a seed edit).
    seed = copy_seed(tmp_path, monkeypatch)
    first = sandbox.seed_sha()
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    assert sandbox.seed_sha() == first

    target = seed / "app" / "utils.py"
    target.write_bytes(target.read_bytes() + b" ")

    assert sandbox.seed_sha() != first


def test_seed_sha_tracks_a_file_rename(tmp_path, monkeypatch):
    # Fails if the path is left out of the hash.
    seed = copy_seed(tmp_path, monkeypatch)
    first = sandbox.seed_sha()
    (seed / "app" / "utils_old.py").rename(seed / "app" / "utils_older.py")
    assert sandbox.seed_sha() != first


def test_build_seed_image_applies_files_then_pip_then_suite(tmp_path, monkeypatch):
    # Fails if the suite runs before pip, or files are not placed under /repo.
    copy_seed(tmp_path, monkeypatch)
    backend = FakeBackend()

    uuid = asyncio.run(sandbox.build_seed_image(backend))

    assert uuid == FINAL_UUID
    kinds = [c[0] for c in backend.calls]
    assert kinds == ["oci", "apply_files", "session.run", "session.run"]
    assert backend.calls[0] == ("oci", "python:3.12-slim")
    applied = backend.calls[1][1]
    assert applied["/repo/app/api.py"] == (tmp_path / "seed" / "app" / "api.py").read_bytes()
    assert "/repo/migrations/0001_init.py" in applied
    assert all(path.startswith("/repo/") for path in applied)
    pip, suite = backend.calls[2][1], backend.calls[3][1]
    assert "pip install" in pip["shell"] and "/repo/requirements.txt" in pip["shell"]
    assert suite["shell"] == "python -m pytest -q -p no:cacheprovider"
    for kw in (pip, suite):
        assert kw["cwd"] == "/repo"
        assert kw["disposable"] is False
        assert kw["timeout"] >= 300


def test_build_seed_image_raises_with_output_tail_when_the_suite_fails(tmp_path, monkeypatch):
    # Fails if a red seed suite still yields an image.
    copy_seed(tmp_path, monkeypatch)
    backend = FakeBackend({"pytest": res(1, "1 failed in 0.1s\n", "boom")})

    with pytest.raises(
        RuntimeError, match=r"the seed suite failed with exit code 1[\s\S]*1 failed"
    ):
        asyncio.run(sandbox.build_seed_image(backend))


def test_build_seed_image_stops_before_the_suite_when_pip_fails(tmp_path, monkeypatch):
    # Fails if a broken install is ignored and the suite runs anyway.
    copy_seed(tmp_path, monkeypatch)
    backend = FakeBackend({"pip install": res(1, "", "No matching distribution")})

    with pytest.raises(RuntimeError, match="pip install failed"):
        asyncio.run(sandbox.build_seed_image(backend))

    assert [c[0] for c in backend.calls] == ["oci", "apply_files", "session.run"]


def test_seed_image_builds_once_then_reuses_the_cache(tmp_path, monkeypatch):
    # Fails if every call rebuilds the image (minutes of sandbox time per run).
    copy_seed(tmp_path, monkeypatch)
    cache = tmp_path / "runs" / "seed.json"
    backend = FakeBackend()

    first = asyncio.run(sandbox.seed_image(backend, cache))
    builds = len([c for c in backend.calls if c[0] == "oci"])
    second = asyncio.run(sandbox.seed_image(backend, cache))

    assert (first, second) == (FINAL_UUID, FINAL_UUID)
    assert builds == 1
    assert len([c for c in backend.calls if c[0] == "oci"]) == 1
    saved = json.loads(cache.read_text())
    assert saved["uuid"] == FINAL_UUID
    assert saved["base_image"] == "python:3.12-slim"
    assert re.fullmatch(r"[0-9a-f]{12}", saved["seed_sha"])


def test_seed_image_rebuilds_when_the_seed_changed(tmp_path, monkeypatch):
    # Fails if a stale image is served after the seed is edited.
    seed = copy_seed(tmp_path, monkeypatch)
    cache = tmp_path / "seed.json"
    cache.write_text(
        json.dumps({"seed_sha": "000000000000", "base_image": "python:3.12-slim", "uuid": "old"})
    )
    backend = FakeBackend()

    assert asyncio.run(sandbox.seed_image(backend, cache)) == FINAL_UUID
    assert json.loads(cache.read_text())["seed_sha"] == sandbox.seed_sha()

    (seed / "app" / "utils.py").write_bytes(b"# edited\n")
    assert asyncio.run(sandbox.seed_image(backend, cache)) == FINAL_UUID
    assert len([c for c in backend.calls if c[0] == "oci"]) == 2


def test_seed_image_rebuilds_when_the_base_image_changed(tmp_path, monkeypatch):
    # Fails if the cache ignores BASE_IMAGE.
    copy_seed(tmp_path, monkeypatch)
    cache = tmp_path / "seed.json"
    cache.write_text(
        json.dumps(
            {"seed_sha": sandbox.seed_sha(), "base_image": "python:3.11-slim", "uuid": "old"}
        )
    )

    assert asyncio.run(sandbox.seed_image(FakeBackend(), cache)) == FINAL_UUID


def test_seed_image_ignores_a_corrupt_cache(tmp_path, monkeypatch):
    # Fails if a half-written cache file crashes every later run.
    copy_seed(tmp_path, monkeypatch)
    cache = tmp_path / "seed.json"
    cache.write_text("{not json")

    assert asyncio.run(sandbox.seed_image(FakeBackend(), cache)) == FINAL_UUID


def test_new_session_starts_on_the_given_image():
    # Fails if the fork starts from the base image instead of the seed image.
    backend = FakeBackend()

    session = asyncio.run(sandbox.new_session(backend, FINAL_UUID))

    assert backend.calls == [("use", UUID(FINAL_UUID))]
    assert session.uuid == FINAL_UUID


def test_task_success_true_runs_suite_then_check_disposably():
    # Fails if the check lands in /repo, runs before the suite, or runs on the session itself.
    backend = FakeBackend()
    session = FakeSession(backend, FINAL_UUID)

    ok, tail = asyncio.run(sandbox.task_success(session, "check_t1.py", b"print('hi')\n"))

    assert (ok, tail) == (True, "")
    assert [c[0] for c in backend.calls] == ["use", "image.run", "image.run"]
    suite, check = backend.calls[1][1], backend.calls[2][1]
    assert suite["shell"] == "python -m pytest -q -p no:cacheprovider"
    assert suite["files"] is None
    assert check["shell"] == "python /nemogate/check_t1.py"
    assert check["files"] == {"/nemogate/check_t1.py": b"print('hi')\n"}
    for kw in (suite, check):
        assert kw["cwd"] == "/repo"
        assert kw["disposable"] is True
        assert kw["env"] == {"PYTHONDONTWRITEBYTECODE": "1"}
        assert kw["timeout"] >= 300


def test_task_success_false_with_the_check_output_when_the_check_exits_1():
    # Fails if a failing check is reported as success or its message is lost.
    backend = FakeBackend({"/nemogate/": res(1, "", "FAIL: no priority column\n")})
    session = FakeSession(backend, FINAL_UUID)

    ok, tail = asyncio.run(sandbox.task_success(session, "check_t1.py", b"raise SystemExit(1)"))

    assert ok is False
    assert tail == "FAIL: no priority column\n"


def test_task_success_skips_the_check_when_the_suite_fails():
    # Fails if a red suite is masked by a passing check.
    backend = FakeBackend({"pytest": res(1, "2 failed\n", "")})
    session = FakeSession(backend, FINAL_UUID)

    ok, tail = asyncio.run(sandbox.task_success(session, "check_t1.py", b""))

    assert (ok, tail) == (False, "2 failed\n")
    assert [c[0] for c in backend.calls] == ["use", "image.run"]


def test_task_success_keeps_only_the_tail_of_long_output():
    # Fails if a huge traceback is passed on whole.
    backend = FakeBackend({"pytest": res(1, "x" * 5000 + "END", "")})
    session = FakeSession(backend, FINAL_UUID)

    ok, tail = asyncio.run(sandbox.task_success(session, "c.py", b""))

    assert ok is False
    assert len(tail) == 2000 and tail.endswith("xxEND")


def test_task_success_reports_a_timeout_as_failure():
    # Fails if a hung check raises instead of counting as task failure.
    timeout = OperationTimedOutError(operation_uuid=UUID(START_UUID))
    backend = FakeBackend({"/nemogate/": timeout})
    session = FakeSession(backend, FINAL_UUID)

    ok, tail = asyncio.run(sandbox.task_success(session, "check_t1.py", b""))

    assert ok is False
    assert tail == "`python /nemogate/check_t1.py` timed out after 300 s"


@pytest.mark.parametrize("name", ["../x.py", "a/b.py", "/etc/x.py"])
def test_task_success_rejects_a_check_name_with_a_path(name):
    # Fails if a check could be written outside CHECK_DIR.
    with pytest.raises(ValueError, match="bare file name"):
        asyncio.run(sandbox.task_success(FakeSession(FakeBackend(), FINAL_UUID), name, b""))


@pytest.mark.parametrize(
    "name",
    ["missing.py; true", "x.py && rm -rf /", "a b.py", "$(id).py", "x.py\n", "check", "x.txt", ""],
)
def test_task_success_rejects_a_check_name_that_is_not_a_plain_python_file(name):
    # Fails if a shell metacharacter in the name reaches the command (`missing.py; true` would
    # make the check vacuous: `python /nemogate/missing.py` fails, `true` exits 0).
    backend = FakeBackend()
    with pytest.raises(ValueError, match="bare file name"):
        asyncio.run(sandbox.task_success(FakeSession(backend, FINAL_UUID), name, b""))

    assert backend.calls == []


def test_task_success_quotes_the_check_path_in_the_command():
    # Pins the command text: shlex.quote leaves every name the validation lets through bare, so
    # the quoting is defence in depth and cannot be observed with a hostile name.
    backend = FakeBackend()

    asyncio.run(sandbox.task_success(FakeSession(backend, FINAL_UUID), "check_t-1.v2.py", b""))

    assert backend.calls[2][1]["shell"] == "python /nemogate/check_t-1.v2.py"


def test_main_refuses_without_the_live_switch(monkeypatch):
    # Fails if the entry point can spend sandbox credit without NEMOGATE_LIVE=1.
    monkeypatch.delenv("NEMOGATE_LIVE", raising=False)
    with pytest.raises(SystemExit, match="NEMOGATE_LIVE=1"):
        sandbox.main()
