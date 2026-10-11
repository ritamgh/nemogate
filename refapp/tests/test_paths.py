import pytest

from refapp.paths import repo_relative


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # under the repo -> repo-relative
        ("/repo/a/b.py", "a/b.py"),
        ("/repo/migrations/0003_x.py", "migrations/0003_x.py"),
        ("/repo", "."),
        ("/repo/", "."),
        ("/repo//a///b.py", "a/b.py"),
        ("//repo/a.py", "a.py"),
        ("/repo/a/../b.py", "b.py"),
        ("/repo/./a.py", "a.py"),
        # relative -> returned normalised
        ("a/b.py", "a/b.py"),
        ("./a//b.py", "a/b.py"),
        ("a/../b.py", "b.py"),
        ("", "."),
        # outside the repo -> normalised absolute path, never repo-relative
        ("/etc/x", "/etc/x"),
        ("/repo/../etc/x", "/etc/x"),
        ("/repository/x", "/repository/x"),
        ("/repo/../repo2/x", "/repo2/x"),
        ("/", "/"),
        # backslashes are separators, as Deep Agents' file tools treat them
        ("/repo\\migrations\\new.py", "migrations/new.py"),
        ("\\repo\\a.py", "a.py"),
        ("/repo\\a/../b.py", "b.py"),
        ("/repo\\..\\etc\\x", "/etc/x"),
        ("a\\b.py", "a/b.py"),
    ],
)
def test_repo_relative_default_repo(path, expected):
    # Fails if a prefix test is plain startswith("/repo") (`/repository/x`), if `..` is not
    # resolved before the prefix test (`/repo/../etc/x`), if the repo root is not ".", or if a
    # backslash is kept as a filename character (Deep Agents turns it into `/` before acting).
    assert repo_relative(path) == expected


@pytest.mark.parametrize(
    ("path", "repo", "expected"),
    [
        ("/work/app/a.py", "/work/app", "a.py"),
        ("/work/app/", "/work/app/", "."),
        ("/work/app/x/y.py", "/work/app/", "x/y.py"),
        ("/work/app2/a.py", "/work/app", "/work/app2/a.py"),
        ("/repo/a.py", "/work/app", "/repo/a.py"),
        # a leading double slash on `repo` is collapsed like the one on `path`
        ("//repo/x.py", "//repo/", "x.py"),
        ("/repo/x.py", "//repo", "x.py"),
        ("//repo/x.py", "/repo", "x.py"),
        ("\\work\\app\\x.py", "/work/app", "x.py"),
    ],
)
def test_repo_relative_custom_repo(path, repo, expected):
    # Fails if the `repo` argument is ignored, a trailing slash on it breaks the prefix test, or
    # `repo` keeps a leading `//` that `path` loses.
    assert repo_relative(path, repo) == expected
