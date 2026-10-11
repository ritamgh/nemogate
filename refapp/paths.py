"""Make an agent's tool-call path comparable with the repo-relative paths of a filesystem diff."""

import posixpath

from refapp.tools import DEFAULT_REPO as REPO


def _normalise(path: str) -> str:
    # Deep Agents' file tools turn every backslash into `/` before acting
    # (deepagents/backends/utils.py: validate_path:893, to_posix_path:841; called from
    # middleware/filesystem.py:207), so `/repo\migrations\x.py` writes `/repo/migrations/x.py`.
    path = path.replace("\\", "/")
    if path.startswith("/"):
        path = "/" + path.lstrip("/")  # normpath keeps a leading `//`
    return posixpath.normpath(path)


def repo_relative(path: str, repo: str = REPO) -> str:
    """`path` relative to `repo` when it lies under it, else its normalised form.

    Agents pass absolute paths (`/repo/migrations/0003_x.py`) while a filesystem diff is
    repo-relative (`migrations/0003_x.py`). Normalising first (`..`, `.`, repeated or leading
    double slashes, backslashes as `/`, on `repo` as well as `path`) means `/repo/../etc/x` is
    seen as `/etc/x`, outside the repo. A path outside the repo comes back as its normalised
    absolute path (and a relative one stays relative), so a repo-relative glob never matches it.
    The repo root itself is `.`.
    """
    path = _normalise(path)
    root = _normalise(repo)
    if path == root:
        return "."
    if path.startswith(root.rstrip("/") + "/"):
        return path[len(root.rstrip("/")) + 1 :]
    return path
