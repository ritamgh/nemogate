"""The research app's sandbox: a tiny repo at /repo and a task check, on the same Contree helpers
as the coding app (`refapp.sandbox`).

The app has no code to edit, so the image is the base Python with one README under /repo (the
filesystem diff of a run is then just what the lead writes) and nothing is installed. The check
runs on its own: there is no repo suite, and pytest is not in the image.
"""

import hashlib
from pathlib import Path

from common.spend import spend_log_path
from refapp import sandbox as coding

REPO = coding.REPO
BASE_IMAGE = coding.BASE_IMAGE
FILES = {
    "README.md": (
        b"# Research workspace\n\n"
        b"Write the finished briefing here, in the file the request names.\n"
    ),
}
_CACHE_NAME = "refapp_research_image.json"


def image_sha() -> str:
    """12 hex chars identifying the image's content: the key of the cached image."""
    digest = hashlib.sha256()
    for path, data in sorted(FILES.items()):
        digest.update(path.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()[:12]


async def build_image(client) -> str:
    """Import the base image and put the workspace files under /repo; return the image uuid."""
    session = (await client.images.oci(BASE_IMAGE)).session()
    await session.apply_files({f"{REPO}/{rel}": data for rel, data in FILES.items()})
    return str(session.uuid)


def default_cache() -> Path:
    """`refapp_research_image.json` next to the shared spend log (`~/.nemogate/` by default)."""
    return spend_log_path().parent / _CACHE_NAME


async def research_image(client, cache: Path | None = None) -> str:
    """The research image's uuid: cached, checked to still exist on Contree, else rebuilt."""
    cache = default_cache() if cache is None else Path(cache)
    return await coding.cached_image(client, cache, image_sha(), build_image)


async def task_success(session, check_name: str, check_bytes: bytes) -> tuple[bool, str]:
    """True when the check exits 0 in the session's current image (see `coding.task_success`)."""
    return await coding.task_success(session, check_name, check_bytes, suite=False)
