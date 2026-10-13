import os

import pytest

import common.env
from common import MissingEnvError
from common.env import is_live, load_dotenv, main_checkout_root, require_env
from common.tests.conftest import make_repo_with_worktree, run_git


def _forget(monkeypatch, *names):
    """Make monkeypatch restore these variables to 'absent' at teardown, then remove them now."""
    for name in names:
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)


def test_dotenv_parses_quotes_comments_and_blanks(tmp_path, monkeypatch):
    # Breaks if quotes stay in the value, or comments/blank lines are parsed as variables.
    env = tmp_path / ".env"
    env.write_text(
        '# a comment\n\nPLAIN=abc\nDQ="two words"\nSQ=\'single\'\nEMPTY=\nEQ=a=b\nMIX="open\n',
        encoding="utf-8",
    )
    _forget(monkeypatch, "PLAIN", "DQ", "SQ", "EMPTY", "EQ", "MIX")
    load_dotenv(env)
    assert os.environ["PLAIN"] == "abc"
    assert os.environ["DQ"] == "two words"
    assert os.environ["SQ"] == "single"
    assert os.environ["EMPTY"] == ""
    assert os.environ["EQ"] == "a=b"
    assert os.environ["MIX"] == '"open'


def test_dotenv_handles_export_prefix_inline_comments_and_text_after_quotes(tmp_path, monkeypatch):
    # Breaks if `export KEY=v` sets a variable named "export KEY", or a trailing ` # note` ends
    # up inside the (credential) value.
    env = tmp_path / ".env"
    env.write_text(
        "export EXPORTED=abc\n"
        "export   SPACED = def \n"
        "UNQUOTED=abc # a note\n"
        'DQ="abc" # a note\n'
        "SQ='a b' trailing\n"
        'DQ_HASH="a # b"\n'
        "NOSPACE=a#b\n"
        "BLANK_THEN_NOTE= # only a note\n",
        encoding="utf-8",
    )
    names = ["EXPORTED", "SPACED", "UNQUOTED", "DQ", "SQ", "DQ_HASH", "NOSPACE", "BLANK_THEN_NOTE"]
    _forget(monkeypatch, *names, "export EXPORTED", "export   SPACED")
    load_dotenv(env)
    assert os.environ["EXPORTED"] == "abc"
    assert os.environ["SPACED"] == "def"
    assert os.environ["UNQUOTED"] == "abc"
    assert os.environ["DQ"] == "abc"
    assert os.environ["SQ"] == "a b"
    assert os.environ["DQ_HASH"] == "a # b"
    assert os.environ["NOSPACE"] == "a#b"
    assert os.environ["BLANK_THEN_NOTE"] == ""
    assert "export EXPORTED" not in os.environ


def test_dotenv_never_overrides_existing_env(tmp_path, monkeypatch):
    # Breaks if a .env value replaces a variable the caller already exported.
    (tmp_path / ".env").write_text("KEEP_ME=from-file\n", encoding="utf-8")
    monkeypatch.setenv("KEEP_ME", "from-shell")
    load_dotenv(tmp_path / ".env")
    assert os.environ["KEEP_ME"] == "from-shell"


def test_dotenv_missing_file_is_a_noop(tmp_path):
    # Breaks if a missing .env raises (CI and tests have none).
    load_dotenv(tmp_path / "absent.env")


def test_require_env_names_missing_and_blank_but_never_values(monkeypatch):
    # Breaks if a blank variable passes (contree-sdk would send the name as the credential),
    # or if the message carries a value.
    monkeypatch.setenv("SET_ONE", "secret-value-123")
    monkeypatch.setenv("BLANK_ONE", "   ")
    _forget(monkeypatch, "ABSENT_ONE")
    with pytest.raises(MissingEnvError) as exc:
        require_env("SET_ONE", "BLANK_ONE", "ABSENT_ONE")
    text = str(exc.value)
    assert "BLANK_ONE" in text and "ABSENT_ONE" in text
    assert "SET_ONE" not in text
    assert "secret-value-123" not in text
    assert isinstance(exc.value, RuntimeError)


def test_require_env_reads_the_repo_root_dotenv(tmp_path, monkeypatch):
    # Breaks if require_env forgets to load .env first.
    root = tmp_path / "root"
    root.mkdir()
    (root / ".env").write_text("FROM_DOTENV_ONLY=ok\n", encoding="utf-8")
    monkeypatch.setattr(common.env, "REPO_ROOT", root)
    _forget(monkeypatch, "FROM_DOTENV_ONLY")
    require_env("FROM_DOTENV_ONLY")


def test_require_env_finds_the_main_checkouts_dotenv_from_a_worktree(tmp_path, monkeypatch):
    # Breaks if .env is read from the current checkout: a live command in a worktree (which has
    # no untracked .env) fails with MissingEnvError.
    main, wt = make_repo_with_worktree(tmp_path)
    (main / ".env").write_text("FROM_MAIN_ONLY=ok\n", encoding="utf-8")
    monkeypatch.setattr(common.env, "REPO_ROOT", wt)
    _forget(monkeypatch, "FROM_MAIN_ONLY")
    require_env("FROM_MAIN_ONLY")
    assert os.environ["FROM_MAIN_ONLY"] == "ok"


def test_main_checkout_root_is_shared_by_every_worktree(monkeypatch, tmp_path):
    # Breaks if a worktree resolves to itself, so .env and the legacy log split per worktree.
    main, wt = make_repo_with_worktree(tmp_path)
    assert main_checkout_root(main).resolve() == main.resolve()
    assert main_checkout_root(wt).resolve() == main.resolve()
    monkeypatch.setattr(common.env, "REPO_ROOT", wt)
    assert main_checkout_root().resolve() == main.resolve()


def test_main_checkout_root_falls_back_outside_a_checkout(tmp_path):
    # Breaks if a tree without git metadata (a tarball) crashes instead of using its own root.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert main_checkout_root(plain) == plain


def test_a_plain_folder_inside_another_repo_does_not_borrow_its_root(tmp_path):
    # Breaks if a checkout without git metadata that sits inside an unrelated repo (e.g. a
    # dotfiles repo in $HOME) resolves to that repo and reads its .env.
    outer = tmp_path / "outer"
    outer.mkdir()
    run_git(outer, "init", "-q")
    plain = outer / "project"
    plain.mkdir()
    assert main_checkout_root(plain) == plain


def test_dotenv_value_never_reaches_output(tmp_path, monkeypatch, capsys, caplog):
    # Breaks if the parser prints or logs what it loads.
    (tmp_path / ".env").write_text("LEAKY_KEY=sentinel-value-9981\n", encoding="utf-8")
    _forget(monkeypatch, "LEAKY_KEY")
    with caplog.at_level("DEBUG"):
        load_dotenv(tmp_path / ".env")
    out = capsys.readouterr()
    assert "sentinel-value-9981" not in out.out + out.err + caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("0", False), ("true", False), ("1 ", False), ("1", True)],
)
def test_is_live_only_for_exactly_1(monkeypatch, value, expected):
    # Breaks if "true" or " 1" switch on paid calls.
    if value is None:
        monkeypatch.delenv("NEMOGATE_LIVE", raising=False)
    else:
        monkeypatch.setenv("NEMOGATE_LIVE", value)
    assert is_live() is expected


def _separate_git_dir_repo(tmp_path):
    """A real repo whose git dir lives away from its work tree: (project, git_dir)."""
    project, git_dir = tmp_path / "project", tmp_path / "elsewhere" / "repo.git"
    project.mkdir()
    git_dir.parent.mkdir()
    run_git(tmp_path, "init", "-q", f"--separate-git-dir={git_dir}", str(project))
    run_git(project, "commit", "-q", "--allow-empty", "-m", "x")
    return project, git_dir


def test_main_checkout_root_is_the_work_tree_when_the_git_dir_is_elsewhere(tmp_path, monkeypatch):
    # Breaks if the root is taken as the parent of the git common dir: it would be
    # tmp_path/elsewhere, and its .env would be loaded instead of the project's.
    project, git_dir = _separate_git_dir_repo(tmp_path)
    wt = tmp_path / "wt"
    run_git(project, "worktree", "add", "-q", str(wt))
    assert main_checkout_root(project).resolve() == project.resolve()
    # git lists the git dir, not the work tree, as the main entry, so a linked worktree of such a
    # repo cannot find the main checkout: it keeps its own root (never the git dir's parent).
    assert main_checkout_root(wt).resolve() == wt.resolve()
    (git_dir.parent / ".env").write_text("FROM_GIT_DIR_PARENT=bad\n", encoding="utf-8")
    (project / ".env").write_text("FROM_PROJECT=ok\n", encoding="utf-8")
    monkeypatch.setattr(common.env, "REPO_ROOT", project)
    _forget(monkeypatch, "FROM_GIT_DIR_PARENT", "FROM_PROJECT")
    require_env("FROM_PROJECT")
    assert "FROM_GIT_DIR_PARENT" not in os.environ


# ---- compaction_from_env --------------------------------------------------------------------


def test_compaction_is_off_when_the_token_variable_is_unset():
    # Breaks if an unset knob turns compaction on (default off: no behaviour change).
    assert common.env.compaction_from_env({}) is None


def test_compaction_reads_tokens_and_keep():
    # Breaks if either number is read from the wrong variable or left a string.
    environ = {"NEMOGATE_COMPACT_TOKENS": "9000", "NEMOGATE_COMPACT_KEEP": "2"}
    assert common.env.compaction_from_env(environ) == (9000, 2)


def test_compaction_keep_defaults_to_the_deep_agents_default_of_six():
    # Breaks if a missing keep is an error or any value other than the library default.
    assert common.env.compaction_from_env({"NEMOGATE_COMPACT_TOKENS": "9000"}) == (9000, 6)


@pytest.mark.parametrize(
    "environ",
    [
        {"NEMOGATE_COMPACT_KEEP": "2"},
        {"NEMOGATE_COMPACT_TOKENS": "lots"},
        {"NEMOGATE_COMPACT_TOKENS": "0"},
        {"NEMOGATE_COMPACT_TOKENS": "9000", "NEMOGATE_COMPACT_KEEP": "-1"},
        {"NEMOGATE_COMPACT_TOKENS": "9000", "NEMOGATE_COMPACT_KEEP": "two"},
    ],
)
def test_compaction_refuses_a_half_set_or_malformed_knob(environ):
    # Breaks if a typo silently runs an ordinary natural run that is then pooled as a compaction
    # run (or the reverse).
    with pytest.raises(ValueError, match="NEMOGATE_COMPACT"):
        common.env.compaction_from_env(environ)
