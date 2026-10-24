"""The research app's sandbox image (a tiny repo, no dependencies) and its task check, against a
fake Contree: nothing here touches the network."""

import asyncio
import json
import re
from types import SimpleNamespace
from uuid import UUID

import pytest
from contree_sdk.sdk.exceptions import NotFoundError, OperationTimedOutError

from refapp import sandbox as seed_sandbox
from refapp.research import sandbox

FINAL_UUID = "11111111-2222-3333-4444-555555555555"
START_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeRun:
    def __init__(self, result):
        self.result = result

    def __await__(self):
        async def get():
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        return get().__await__()


class FakeBackend:
    """Records SDK calls in order; `results` maps a substring of a shell command to its result."""

    def __init__(self, results=None, missing=()):
        self.calls: list[tuple] = []
        self.results = results or {}
        self.missing = {str(u) for u in missing}
        self.images = SimpleNamespace(oci=self._oci, use=self._use)

    async def _oci(self, ref):
        self.calls.append(("oci", ref))
        return FakeImage(self, START_UUID)

    async def _use(self, uuid, **kw):
        self.calls.append(("use", uuid) + ((kw,) if kw else ()))
        if kw.get("strict") and str(uuid) in self.missing:
            raise NotFoundError()
        return FakeImage(self, str(uuid))

    def result_for(self, shell):
        for needle, result in self.results.items():
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
        self.uuid = FINAL_UUID  # applying files makes a new image (docs/FACTS.md section 3)

    def run(self, **kw):
        self.backend.calls.append(("session.run", kw))
        return FakeRun(self.backend.result_for(kw["shell"]))


def res(code, out="", err=""):
    return SimpleNamespace(exit_code=code, stdout=out, stderr=err)


def test_the_image_is_the_base_python_with_one_readme_under_repo_and_nothing_installed():
    # Breaks if the image grows a dependency install or a suite step (the app has no repo to test)
    # or puts the files anywhere but /repo, which the runner's filesystem diff is relative to.
    backend = FakeBackend()
    assert asyncio.run(sandbox.build_image(backend)) == FINAL_UUID
    assert [c[0] for c in backend.calls] == ["oci", "apply_files"]
    assert backend.calls[0] == ("oci", "python:3.12-slim")
    applied = backend.calls[1][1]
    assert list(applied) == ["/repo/README.md"]
    assert applied["/repo/README.md"].startswith(b"# Research workspace")


def test_image_sha_is_12_hex_and_tracks_the_files(monkeypatch):
    # Breaks if an edit to the workspace files reuses a stale cached image.
    first = sandbox.image_sha()
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    monkeypatch.setitem(sandbox.FILES, "README.md", sandbox.FILES["README.md"] + b" ")
    assert sandbox.image_sha() != first


def test_research_image_builds_once_then_reuses_a_cached_image_that_still_exists(tmp_path):
    # Breaks if every run rebuilds the image, or trusts a cached uuid Contree no longer has.
    cache = tmp_path / "research.json"
    backend = FakeBackend()
    assert asyncio.run(sandbox.research_image(backend, cache)) == FINAL_UUID
    saved = json.loads(cache.read_text())
    assert saved == {
        "seed_sha": sandbox.image_sha(),
        "base_image": "python:3.12-slim",
        "uuid": FINAL_UUID,
    }
    assert asyncio.run(sandbox.research_image(backend, cache)) == FINAL_UUID
    assert len([c for c in backend.calls if c[0] == "oci"]) == 1
    assert backend.calls[-1] == ("use", UUID(FINAL_UUID), {"strict": True})

    gone = FakeBackend(missing=[FINAL_UUID])
    assert asyncio.run(sandbox.research_image(gone, cache)) == FINAL_UUID
    assert [c[0] for c in gone.calls] == ["use", "oci", "apply_files"]


def test_the_cache_file_is_not_the_coding_apps(tmp_path, monkeypatch):
    # Breaks if both apps share one cache file: each would keep rebuilding the other's image.
    monkeypatch.setenv("NEMOGATE_SPEND_LOG", str(tmp_path / "spend.jsonl"))
    assert sandbox.default_cache() == tmp_path / "refapp_research_image.json"
    assert sandbox.default_cache() != seed_sandbox._default_cache()


def test_task_success_runs_only_the_check_disposably_on_the_final_image():
    # Breaks if the repo suite is run (there is none, and pytest is not installed), if the check
    # lands in /repo, or if it runs on the session itself and breaks it (FACTS section 7).
    backend = FakeBackend()
    session = FakeSession(backend, FINAL_UUID)
    ok, tail = asyncio.run(sandbox.task_success(session, "check_t13.py", b"print('hi')\n"))
    assert (ok, tail) == (True, "")
    assert [c[0] for c in backend.calls] == ["use", "image.run"]
    kw = backend.calls[1][1]
    assert kw["shell"] == "python /nemogate/check_t13.py"
    assert kw["cwd"] == "/repo" and kw["disposable"] is True
    assert kw["files"] == {"/nemogate/check_t13.py": b"print('hi')\n"}


def test_task_success_returns_the_failing_output_tail():
    # Breaks if a failing check reads as success, or the reason is lost.
    backend = FakeBackend({"check_t13.py": res(1, "briefing.md was not written\n")})
    ok, tail = asyncio.run(
        sandbox.task_success(FakeSession(backend, FINAL_UUID), "check_t13.py", b"x")
    )
    assert (ok, tail) == (False, "briefing.md was not written\n")


def test_task_success_reports_a_timeout_as_a_failure():
    # Breaks if a hung check crashes the run's scoring instead of failing the task.
    backend = FakeBackend({"check_t13.py": OperationTimedOutError(operation_uuid=UUID(START_UUID))})
    ok, tail = asyncio.run(
        sandbox.task_success(FakeSession(backend, FINAL_UUID), "check_t13.py", b"x")
    )
    assert ok is False and "timed out" in tail


def test_task_success_refuses_a_check_name_that_is_not_a_bare_file_name():
    # Breaks if a task.yaml can make the sandbox run a path outside /nemogate.
    with pytest.raises(ValueError, match="check_name"):
        asyncio.run(
            sandbox.task_success(FakeSession(FakeBackend(), FINAL_UUID), "../check.py", b"x")
        )
