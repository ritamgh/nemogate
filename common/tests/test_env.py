import os

import pytest

import common.env
from common import MissingEnvError
from common.env import is_live, load_dotenv, require_env


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
