"""Snapshot the repo inside a Contree image and diff two snapshots (replay/README.md, fsdiff.py)."""

import json
import shlex
from uuid import UUID

from schemas.enums import FsOp
from schemas.run import FsChange

SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache"})
SKIP_SUFFIXES = frozenset({".pyc"})

# A regular file that is empty or only ASCII whitespace (bytes.strip()) within its first
# BLANK_LIMIT bytes is "blank". The snapshot marks it with BLANK_MARK after its hash, so equal
# values still mean the same bytes and the value of every other file is the plain hash.
BLANK_LIMIT = 64 * 1024
BLANK_MARK = ":blank"

# Runs inside the image under its own `python3`, so it sticks to the standard library and
# syntax any python3 has. argv[1] is the root. Output is one JSON object, ASCII only
# (`ensure_ascii`), so any file name (spaces, newlines, unicode) survives the SDK's strict
# UTF-8 decode of stdout (FACTS §3, "Output decoding"). Symlinks are recorded, never followed
# (os.walk lists a symlink to a directory among `dirs`; it is recorded by the lstat check, not
# entered). Only regular files are hashed (a fifo would block); a missing or unreadable root
# raises.
_TEMPLATE = """
import hashlib, json, os, stat, sys

SKIP_DIRS = @DIRS@
SKIP_SUFFIXES = @SUFFIXES@
BLANK_LIMIT = @LIMIT@
BLANK_MARK = @MARK@
root = sys.argv[1]
if not os.path.isdir(root):
    raise SystemExit("snapshot root is not a directory: " + root)


def fail(err):
    raise err


out = {}
for here, dirs, files in os.walk(root, onerror=fail, followlinks=False):
    dirs.sort()
    prune = set()
    for name in dirs + files:
        path = os.path.join(here, name)
        mode = os.lstat(path).st_mode
        if stat.S_ISDIR(mode):
            # SKIP_DIRS name real directories only; the walk never enters a skipped one
            if name in SKIP_DIRS:
                prune.add(name)
            continue
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if stat.S_ISLNK(mode):
            out[rel] = "symlink:" + os.readlink(path)
        elif stat.S_ISREG(mode) and not name.endswith(SKIP_SUFFIXES):
            # SKIP_SUFFIXES name regular files only
            h = hashlib.sha256()
            size = 0
            blank = True
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                    size += len(chunk)
                    if blank and size > BLANK_LIMIT:
                        blank = False
                    elif blank and chunk.strip():
                        blank = False
            out[rel] = "sha256:" + h.hexdigest() + (BLANK_MARK if blank else "")
    dirs[:] = [d for d in dirs if d not in prune]
print(json.dumps(out))
"""
SCRIPT = (
    _TEMPLATE.replace("@DIRS@", repr(tuple(sorted(SKIP_DIRS))))
    .replace("@SUFFIXES@", repr(tuple(sorted(SKIP_SUFFIXES))))
    .replace("@LIMIT@", repr(BLANK_LIMIT))
    .replace("@MARK@", repr(BLANK_MARK))
)

# Output cap for the snapshot command. The SDK's own default is 65535 bytes
# (contree_sdk/config.py:46), too small for a repo with many files; a hit is reported as an
# error below, never parsed. Same value the sandbox backend uses (FACTS §3).
_MAX_OUTPUT_BYTES = 10 * 1024 * 1024


async def snapshot(client, image_uuid: str, root: str, *, timeout: int = 120) -> dict[str, str]:
    """Map every file under `root` in the image to "sha256:<hex>", symlinks to "symlink:<target>".

    A blank file (empty or whitespace-only, see BLANK_LIMIT) gets BLANK_MARK after its hash.

    One disposable command on a plain image, so the image is untouched (FACTS §3).
    """
    image = await client.images.use(UUID(image_uuid))
    # `run` keywords: contree_sdk/sdk/objects/image_like/_base.py:126-143 (shell, timeout,
    # disposable, truncate_output_at). Awaiting it returns an image in state SUCCEEDED with
    # `.exit_code` (:416), `.stdout`/`.stderr` (:406-413, str) and `.result.truncated`
    # (contree_sdk/sdk/objects/image_like/result.py:21-26).
    done = await image.run(
        shell=f"python3 -c {shlex.quote(SCRIPT)} {shlex.quote(root)}",
        disposable=True,
        timeout=timeout,
        truncate_output_at=_MAX_OUTPUT_BYTES,
    )
    if done.exit_code != 0:
        raise RuntimeError(
            f"snapshot of {image_uuid}:{root} failed with exit code {done.exit_code}: "
            f"{str(done.stderr)[-500:]}"
        )
    if done.result.truncated:
        raise RuntimeError(f"snapshot of {image_uuid}:{root} output was truncated")
    try:
        files = json.loads(done.stdout)
    except ValueError as exc:
        raise RuntimeError(f"snapshot of {image_uuid}:{root} printed invalid JSON: {exc}") from exc
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        raise RuntimeError(f"snapshot of {image_uuid}:{root} printed JSON that is not a path map")
    return files


def _is_blank(value: str) -> bool:
    return value.startswith("sha256:") and value.endswith(BLANK_MARK)


def _is_regular(value: str) -> bool:
    return value.startswith("sha256:")


def diff(base: dict[str, str], final: dict[str, str]) -> list[FsChange]:
    """Added, deleted and modified paths between two snapshots, sorted by path.

    A modified regular file that is blank in `final` and was not blank in `base` is `emptied`
    (an edit that wiped it instead of deleting it). Added and deleted paths never are.
    """
    changes = []
    for path in sorted(base.keys() | final.keys()):
        if path not in base:
            changes.append(FsChange(path=path, op=FsOp.added))
        elif path not in final:
            changes.append(FsChange(path=path, op=FsOp.deleted))
        elif base[path] != final[path]:
            emptied = (
                _is_blank(final[path]) and _is_regular(base[path]) and not _is_blank(base[path])
            )
            changes.append(FsChange(path=path, op=FsOp.modified, emptied=emptied))
    return changes
