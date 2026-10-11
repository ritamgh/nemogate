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
    ],
)
def test_repo_relative_default_repo(path, expected):
    # Fails if a prefix test is plain startswith("/repo") (`/repository/x`), if `..` is not
    # resolved before the prefix test (`/repo/../etc/x`), or if the repo root is not ".".
    assert repo_relative(path) == expected


@pytest.mark.parametrize(
    ("path", "repo", "expected"),
    [
        ("/work/app/a.py", "/work/app", "a.py"),
        ("/work/app/", "/work/app/", "."),
        ("/work/app/x/y.py", "/work/app/", "x/y.py"),
        ("/work/app2/a.py", "/work/app", "/work/app2/a.py"),
        ("/repo/a.py", "/work/app", "/repo/a.py"),
    ],
)
def test_repo_relative_custom_repo(path, repo, expected):
    # Fails if the `repo` argument is ignored or a trailing slash on it breaks the prefix test.
    assert repo_relative(path, repo) == expected
