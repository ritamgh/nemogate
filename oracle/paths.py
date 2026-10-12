"""Make tool-call paths comparable with a filesystem diff, and match ledger globs against them."""

import fnmatch
import posixpath

PATH_ARG = "file_path"  # the path argument of Deep Agents' file tools (docs/FACTS.md section 4)


def _normalise(path: str) -> str:
    # Deep Agents' file tools turn backslashes into `/` before acting (refapp/paths.py notes).
    path = path.replace("\\", "/")
    if path.startswith("/"):
        path = "/" + path.lstrip("/")  # normpath keeps a leading `//`
    return posixpath.normpath(path)


def repo_relative(path: str, root: str | None) -> str:
    """`path` relative to `root` when under it ("." for the root), else its normalised form."""
    path = _normalise(path)
    if root is None:
        return path
    root = _normalise(root)
    if path == root:
        return "."
    prefix = root.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def glob_matches(glob: str, path: str) -> bool:
    """fnmatch on repo-relative paths: `*` and `**` both cross `/`.

    A path also matches when something under it would, so deleting `vendor` matches `vendor/**`.
    """
    return fnmatch.fnmatchcase(path, glob) or fnmatch.fnmatchcase(path.rstrip("/") + "/", glob)
