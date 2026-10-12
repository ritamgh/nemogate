import pytest

from oracle import glob_matches, repo_relative


@pytest.mark.parametrize(
    ("path", "root", "expected"),
    [
        ("/repo/a/b.py", "/repo", "a/b.py"),
        ("/repo", "/repo", "."),
        ("/repo/", "/repo/", "."),
        ("./a//b.py", "/repo", "a/b.py"),
        ("/etc/x", "/repo", "/etc/x"),
        ("/repo/../etc/x", "/repo", "/etc/x"),
        ("/repository/x", "/repo", "/repository/x"),
        ("/repo\\m\\x.py", "/repo", "m/x.py"),
        ("//repo/a.py", "/repo", "a.py"),
        ("/a/b.py", "/", "a/b.py"),
        ("m/../a.py", None, "a.py"),
    ],
)
def test_repo_relative(path, root, expected):
    assert repo_relative(path, root) == expected


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        ("vendor/**", "vendor/x.py", True),
        ("vendor/**", "vendor/a/b.py", True),
        ("vendor/**", "vendor", True),  # deleting the folder deletes what the glob covers
        ("vendor/**", "src/vendor/x.py", False),
        ("vendor/**", "/etc/vendor/x", False),
        ("deps.txt", "deps.txt", True),
        ("deps.txt", "deps.txt.bak", False),
        ("*.lock", "a/b.lock", True),  # documented: * crosses /
    ],
)
def test_glob_matches(glob, path, expected):
    assert glob_matches(glob, path) is expected
