"""Put the seed repo into a Contree sandbox and judge task success there.

A sandbox is a session on an image (docs/FACTS.md section 3). `build_seed_image` makes the seed
image once; every run then starts a fresh session on it with `new_session` (the fork primitive).
Every non-disposable run makes a new image and a command that hits its timeout kills the session,
so every command here passes an explicit timeout.

Live use is `NEMOGATE_LIVE=1 uv run python -m refapp.sandbox`, which builds (or reuses) the image
and prints its uuid.
"""

import asyncio
import hashlib
import json
import re
import shlex
import sys
from pathlib import Path
from uuid import UUID

from contree_sdk.sdk.exceptions import OperationTimedOutError

from common import env
from common.spend import spend_log_path
from refapp import seedenv

REPO = "/repo"
BASE_IMAGE = "python:3.12-slim"
CHECK_DIR = "/nemogate"  # outside the repo, so a check never shows up in the agents' repo

PIP_TIMEOUT_S = 900
SUITE_TIMEOUT_S = 300
CHECK_TIMEOUT_S = 300
_TAIL_CHARS = 2000
_SUITE_CMD = "python -m pytest -q -p no:cacheprovider"
_PIP_CMD = (
    f"python -m pip install --no-cache-dir --disable-pip-version-check -r {REPO}/requirements.txt"
)
_NO_BYTECODE = {"PYTHONDONTWRITEBYTECODE": "1"}  # keep /repo free of __pycache__
_CACHE_NAME = "refapp_seed_image.json"
_SKIP_DIRS = {"__pycache__", ".pytest_cache"}
_CHECK_NAME = re.compile(r"[A-Za-z0-9_.-]+\.py")


def seed_files() -> dict[str, bytes]:
    """Relative path -> bytes for every file of the seed, without caches."""
    return {
        p.relative_to(seedenv.SEED_DIR).as_posix(): p.read_bytes()
        for p in sorted(seedenv.SEED_DIR.rglob("*"))
        if p.is_file()
        and not _SKIP_DIRS.intersection(p.relative_to(seedenv.SEED_DIR).parts)
        and p.suffix != ".pyc"
    }


def seed_sha() -> str:
    """12 hex chars identifying the seed's content; goes into `RunRecord.repo_sha`."""
    digest = hashlib.sha256()
    for path, data in sorted(seed_files().items()):
        digest.update(path.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()[:12]


def _tail(result) -> str:
    return ((result.stdout or "") + (result.stderr or ""))[-_TAIL_CHARS:]


async def build_seed_image(client) -> str:
    """Import the base image, copy the seed to /repo, install its requirements, run its suite.

    Returns the uuid of the final image. Raises RuntimeError with the output tail when pip or the
    seed's own suite fails.
    """
    session = (await client.images.oci(BASE_IMAGE)).session()
    await session.apply_files({f"{REPO}/{rel}": data for rel, data in seed_files().items()})
    steps = (
        ("pip install", _PIP_CMD, PIP_TIMEOUT_S),
        ("the seed suite", _SUITE_CMD, SUITE_TIMEOUT_S),
    )
    for label, cmd, timeout in steps:
        result = await session.run(
            shell=cmd, cwd=REPO, env=_NO_BYTECODE, timeout=timeout, disposable=False
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"{label} failed with exit code {result.exit_code}:\n{_tail(result)}"
            )
    return str(session.uuid)


def _default_cache() -> Path:
    """`runs/refapp_seed_image.json` of the main checkout, next to the shared spend log."""
    return spend_log_path().parent / _CACHE_NAME


async def seed_image(client, cache: Path | None = None) -> str:
    """The seed image's uuid: from `cache` when its seed sha and base image match, else rebuilt."""
    cache = _default_cache() if cache is None else Path(cache)
    sha = seed_sha()
    try:
        saved = json.loads(cache.read_text())
        if saved["seed_sha"] == sha and saved["base_image"] == BASE_IMAGE:
            return str(saved["uuid"])
    except (OSError, ValueError, KeyError, TypeError):
        pass  # no usable cache: build
    uuid = await build_seed_image(client)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"seed_sha": sha, "base_image": BASE_IMAGE, "uuid": uuid}))
    return uuid


async def new_session(client, image_uuid: str):
    """A fresh async session on `image_uuid`: the fork and rollback primitive (FACTS section 3)."""
    return (await client.images.use(UUID(image_uuid))).session()


async def task_success(session, check_name: str, check_bytes: bytes) -> tuple[bool, str]:
    """True when the repo's suite and then the check both exit 0 in the session's current image.

    Both commands run disposable, on a plain image made from `session.uuid`: nothing is written
    back, so `session` neither advances nor gains files, and nothing is left in /repo. (A
    disposable run on the session itself would clear its uuid, FACTS section 3, so it is not
    used.) The check is attached to the second run at CHECK_DIR/<check_name>. The str is the tail
    of the failing step's output, or a timeout notice; "" on success.
    """
    if not _CHECK_NAME.fullmatch(check_name):
        raise ValueError(f"check_name must be a bare file name like check_t1.py: {check_name!r}")
    check_path = f"{CHECK_DIR}/{check_name}"
    image = await session.client.images.use(session.uuid)
    steps = (
        (_SUITE_CMD, SUITE_TIMEOUT_S, None),
        (
            f"python {shlex.quote(check_path)}",
            CHECK_TIMEOUT_S,
            {check_path: check_bytes},
        ),
    )
    for cmd, timeout, files in steps:
        try:
            result = await image.run(
                shell=cmd,
                cwd=REPO,
                env=_NO_BYTECODE,
                files=files,
                timeout=timeout,
                disposable=True,
            )
        except OperationTimedOutError:
            return False, f"`{cmd}` timed out after {timeout} s"
        if result.exit_code != 0:
            return False, _tail(result)
    return True, ""


def main() -> None:
    """Build (or reuse) the seed image and print its uuid. Needs NEMOGATE_LIVE=1."""
    if not env.is_live():
        sys.exit("refusing to touch Contree without NEMOGATE_LIVE=1 (it uses the sandbox account)")
    env.require_env("NEBIUS_API_KEY", "NEBIUS_PROJECT_ID")
    from contree_sdk import Contree

    async def run() -> str:
        return await seed_image(Contree())

    print(f"seed_sha={seed_sha()} base_image={BASE_IMAGE} image_uuid={asyncio.run(run())}")


if __name__ == "__main__":
    main()
