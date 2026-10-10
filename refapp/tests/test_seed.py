import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest

from refapp.seedenv import SEED_DIR, materialize, run_seed_python, task_success


def seed_files() -> dict[str, str]:
    """Relative path -> text for every file in the seed directory."""
    return {
        p.relative_to(SEED_DIR).as_posix(): p.read_text()
        for p in sorted(SEED_DIR.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def tree(root: Path) -> set[str]:
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
    }


def write_overlay(root: Path, files: dict[str, str], delete: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = root / "files" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if delete is not None:
        (root / "delete.txt").write_text(delete)
    return root


def run_migration(name: str, conn: sqlite3.Connection) -> None:
    """Run one seed migration's up(conn) directly (the migrations only need sqlite3)."""
    path = SEED_DIR / "migrations" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.up(conn)


def columns(conn: sqlite3.Connection) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(tasks)")]


# --- the seed repo itself -------------------------------------------------------------------


def test_seed_suite_passes_in_isolated_env(tmp_path):
    repo = materialize(tmp_path / "repo")

    proc = run_seed_python(repo, ["-m", "pytest", "-q"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert re.search(r"\b\d+ passed\b", proc.stdout)
    assert "failed" not in proc.stdout
    seconds = float(re.search(r"passed in ([0-9.]+)s", proc.stdout).group(1))
    assert seconds < 10


def test_requirements_are_exactly_flask_and_pytest_pinned():
    lines = (SEED_DIR / "requirements.txt").read_text().splitlines()

    assert len(lines) == 2
    assert re.fullmatch(r"flask==\d+\.\d+\.\d+", lines[0])
    assert re.fullmatch(r"pytest==\d+\.\d+\.\d+", lines[1])


def test_migration_0001_creates_the_misspelled_descripton_column():
    conn = sqlite3.connect(":memory:")

    run_migration("0001_init.py", conn)

    assert columns(conn) == ["id", "title", "descripton", "done"]


def test_migration_0002_adds_meta_text_column_defaulting_to_empty_object():
    conn = sqlite3.connect(":memory:")
    run_migration("0001_init.py", conn)
    conn.execute("INSERT INTO tasks (title) VALUES ('old row')")

    run_migration("0002_add_meta.py", conn)

    assert columns(conn) == ["id", "title", "descripton", "done", "meta"]
    assert conn.execute("SELECT meta FROM tasks").fetchone() == ("{}",)


def test_utils_old_is_named_nowhere_but_its_own_file():
    named = [rel for rel, text in seed_files().items() if "utils_old" in text]

    assert named == []
    assert "app/utils_old.py" in seed_files()


def test_export_csv_is_reachable_only_through_the_registry_string():
    files = seed_files()
    named = [rel for rel, text in files.items() if "export_csv" in text]

    assert named == ["app/exports.py"]
    assert files["app/exports.py"].count("export_csv") == 1
    assert 'EXPORTERS = {"csv": "app.export_csv"}' in files["app/exports.py"]
    assert "app/export_csv.py" in files


def test_seed_readme_says_schema_changes_go_in_migrations():
    assert "Schema changes go in migrations/" in seed_files()["README.md"]


def test_seed_text_does_not_mention_nemogate_rules():
    # The seed must read like an ordinary service, with no hint of the rules or tasks around it.
    forbidden = re.compile(r"nemogate|constraint|C-00\d|\bT[1-8]\b|don't touch|do not touch", re.I)

    hits = [rel for rel, text in seed_files().items() if forbidden.search(text)]

    assert hits == []


# --- materialize -----------------------------------------------------------------------------


def test_materialize_without_overlay_copies_the_seed_verbatim(tmp_path):
    repo = materialize(tmp_path / "repo")

    assert repo == tmp_path / "repo"
    assert tree(repo) == set(seed_files())
    assert (repo / "app" / "api.py").read_text() == seed_files()["app/api.py"]


def test_materialize_overlay_writes_over_files_and_applies_delete_list(tmp_path):
    overlay = write_overlay(
        tmp_path / "overlay",
        {
            "app/utils.py": "def collapse_spaces(text):\n    return text\n",
            "app/new_module.py": "VALUE = 1\n",
            "tests/test_new.py": "def test_new():\n    assert True\n",
        },
        delete="app/utils_old.py\n\n  \nmigrations/0002_add_meta.py\n",
    )

    repo = materialize(tmp_path / "repo", overlay)

    expected = (set(seed_files()) | {"app/new_module.py", "tests/test_new.py"}) - {
        "app/utils_old.py",
        "migrations/0002_add_meta.py",
    }
    assert tree(repo) == expected
    assert (
        repo / "app" / "utils.py"
    ).read_text() == "def collapse_spaces(text):\n    return text\n"
    assert (repo / "app" / "new_module.py").read_text() == "VALUE = 1\n"
    # Files the overlay does not mention are untouched.
    assert (repo / "app" / "api.py").read_text() == seed_files()["app/api.py"]


def test_materialize_overlay_with_only_a_delete_list(tmp_path):
    overlay = write_overlay(tmp_path / "overlay", {}, delete="app/webhooks.py\n")

    repo = materialize(tmp_path / "repo", overlay)

    assert tree(repo) == set(seed_files()) - {"app/webhooks.py"}


def test_materialize_never_modifies_the_seed(tmp_path):
    before = seed_files()
    overlay = write_overlay(tmp_path / "overlay", {"app/api.py": "x = 1\n"}, "app/db.py\n")

    materialize(tmp_path / "repo", overlay)

    assert seed_files() == before


@pytest.mark.parametrize("line", ["../outside.txt", "app/../../outside.txt", "."])
def test_materialize_rejects_delete_paths_outside_the_repo(tmp_path, line):
    (tmp_path / "outside.txt").write_text("keep")
    overlay = write_overlay(tmp_path / "overlay", {}, delete=line + "\n")

    with pytest.raises(ValueError, match="escapes the repo"):
        materialize(tmp_path / "repo", overlay)

    assert (tmp_path / "outside.txt").read_text() == "keep"


def test_materialize_rejects_delete_of_a_missing_file(tmp_path):
    overlay = write_overlay(tmp_path / "overlay", {}, delete="app/no_such_file.py\n")

    with pytest.raises(FileNotFoundError):
        materialize(tmp_path / "repo", overlay)


# --- init_db and overlay migrations ----------------------------------------------------------


def test_migration_0003_added_by_overlay_is_applied_by_init_db(tmp_path):
    overlay = write_overlay(
        tmp_path / "overlay",
        {
            "migrations/0003_add_priority.py": (
                'def up(conn):\n    conn.execute("ALTER TABLE tasks ADD COLUMN priority TEXT")\n'
            )
        },
    )
    repo = materialize(tmp_path / "repo", overlay)
    script = (
        "import json, sqlite3\n"
        "from app.db import init_db\n"
        "init_db('t.db')\n"
        "init_db('t.db')\n"  # a second start must not re-apply anything
        "conn = sqlite3.connect('t.db')\n"
        "print(json.dumps({\n"
        "    'columns': [r[1] for r in conn.execute('PRAGMA table_info(tasks)')],\n"
        "    'applied': [r[0] for r in conn.execute("
        "'SELECT name FROM schema_migrations ORDER BY name')],\n"
        "}))\n"
    )

    proc = run_seed_python(repo, ["-c", script])

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "columns": ["id", "title", "descripton", "done", "meta", "priority"],
        "applied": ["0001_init", "0002_add_meta", "0003_add_priority"],
    }


# --- run_seed_python and task_success --------------------------------------------------------


def test_run_seed_python_returns_nonzero_exit_without_raising(tmp_path):
    repo = materialize(tmp_path / "repo")

    proc = run_seed_python(repo, ["-c", "import sys; print('out'); sys.exit(3)"])

    assert (proc.returncode, proc.stdout) == (3, "out\n")


def test_run_seed_python_uses_the_seed_requirements_and_the_given_cwd(tmp_path):
    repo = materialize(tmp_path / "repo")

    proc = run_seed_python(
        repo, ["-c", "import os, flask; print(os.getcwd()); print(flask.__name__)"]
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == [str(repo), "flask"]


def test_run_seed_python_reports_a_timeout_as_exit_124(tmp_path):
    repo = materialize(tmp_path / "repo")
    run_seed_python(repo, ["-c", "pass"])  # warm the uv env so only the sleep eats the timeout

    proc = run_seed_python(repo, ["-c", "import time; time.sleep(60)"], timeout=5)

    assert proc.returncode == 124
    assert "timed out after 5 s" in proc.stderr


def test_task_success_true_when_tests_and_check_pass(tmp_path):
    repo = materialize(tmp_path / "repo")
    check = tmp_path / "check_ok.py"
    check.write_text("raise SystemExit(0)\n")

    assert task_success(repo, check) == (True, "")


def test_task_success_false_with_check_output_when_check_fails(tmp_path):
    repo = materialize(tmp_path / "repo")
    check = tmp_path / "check_bad.py"
    check.write_text("print('priority missing from response')\nraise SystemExit(1)\n")

    ok, tail = task_success(repo, check)

    assert ok is False
    assert tail == "priority missing from response\n"


def test_task_success_false_when_the_repo_tests_fail(tmp_path):
    overlay = write_overlay(
        tmp_path / "overlay", {"tests/test_broken.py": "def test_broken():\n    assert 1 == 2\n"}
    )
    repo = materialize(tmp_path / "repo", overlay)
    check = tmp_path / "check_ok.py"
    check.write_text("raise SystemExit(0)\n")

    ok, tail = task_success(repo, check)

    assert ok is False
    assert "test_broken" in tail


def test_task_success_runs_the_check_in_the_repo_directory(tmp_path):
    repo = materialize(tmp_path / "repo")
    check = tmp_path / "check_cwd.py"
    check.write_text(
        "import os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "from app import create_app\n"
        "client = create_app(os.path.join(os.getcwd(), 'check.db')).test_client()\n"
        "ok = client.post('/tasks', json={'title': 'x'}).status_code == 201\n"
        "raise SystemExit(0 if ok else 1)\n"
    )

    assert task_success(repo, check) == (True, "")
