"""Environment access: the main checkout's `.env`, required variables, and the live switch.

This is the only place that reads `.env` (AGENTS.md rule 2). Values are never printed or logged.
"""

import os
import re
import subprocess
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class MissingEnvError(RuntimeError):
    """A required environment variable is unset or blank. The message names it, never a value."""


@lru_cache
def _main_checkout_root(repo_root: Path) -> Path:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, timeout=5, check=True
        ).stdout

    try:
        # Only trust git when repo_root is itself the checkout, not a folder inside another repo.
        if Path(git("rev-parse", "--show-toplevel").strip()).resolve() != repo_root.resolve():
            return repo_root
        # The first entry of the worktree list is the main worktree. With a separate git dir
        # (`git init --separate-git-dir`) git lists the git dir itself there, which holds no
        # `.git` entry: that is not a work tree, so keep repo_root rather than load its `.env`.
        first = git("worktree", "list", "--porcelain").split("\n", 1)[0]
        if first.startswith("worktree "):
            main = Path(first.removeprefix("worktree "))
            if (main / ".git").exists():
                return main
    except (OSError, subprocess.SubprocessError):
        pass
    return repo_root


def main_checkout_root(repo_root: Path | None = None) -> Path:
    """The main checkout of this repo, so every git worktree shares its `.env` and legacy spend log.

    Falls back to `repo_root` (default `REPO_ROOT`) outside a git checkout.
    """
    return _main_checkout_root(REPO_ROOT if repo_root is None else repo_root)


def _parse_value(raw: str) -> str:
    """A `.env` value: the text between matching quotes, else everything before ` #`."""
    text = raw.lstrip()
    if text[:1] in ("'", '"'):
        end = text.find(text[0], 1)
        if end != -1:
            return text[1:end]
    return re.split(r"\s#", raw, maxsplit=1)[0].strip()


def load_dotenv(path: Path | None = None) -> None:
    """Load `[export ]KEY=VALUE` lines from the main checkout's `.env` without overriding the env.

    A quoted value ends at its closing quote (anything after is ignored); an unquoted value ends
    at the first whitespace-then-`#`.
    """
    env_file = main_checkout_root() / ".env" if path is None else Path(path)
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = re.sub(r"^export\s+", "", line)
        name, _, value = line.partition("=")
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = _parse_value(value)


def require_env(*names: str) -> None:
    """Raise MissingEnvError naming every variable that is unset or whitespace-only.

    contree-sdk builds without error when a variable is unset and sends the variable's name as
    the credential (docs/FACTS.md §9), so callers check first.
    """
    load_dotenv()
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        raise MissingEnvError(f"Missing or blank environment variables: {', '.join(missing)}")


def is_live() -> bool:
    """True only when NEMOGATE_LIVE is exactly "1": the opt-in for real, paid model calls."""
    return os.environ.get("NEMOGATE_LIVE") == "1"


COMPACT_TOKENS_ENV = "NEMOGATE_COMPACT_TOKENS"
COMPACT_KEEP_ENV = "NEMOGATE_COMPACT_KEEP"
DEFAULT_COMPACT_KEEP = 6  # Deep Agents' own default for how many recent messages survive


def compaction_from_env(environ: Mapping[str, str] | None = None) -> tuple[int, int] | None:
    """The planner-compaction knob as `(trigger_tokens, keep_messages)`, or None when it is off.

    Off unless NEMOGATE_COMPACT_TOKENS is set. NEMOGATE_COMPACT_KEEP defaults to 6. A keep without
    a trigger, or a value that is not a positive integer, is a ValueError naming the variable, so
    a typo cannot turn a compaction run into an ordinary natural run unnoticed.
    """
    environ = os.environ if environ is None else environ

    def read(name: str) -> int | None:
        raw = environ.get(name, "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value < 1:
            raise ValueError(f"{name} must be a positive integer, got {raw!r}")
        return value

    tokens, keep = read(COMPACT_TOKENS_ENV), read(COMPACT_KEEP_ENV)
    if tokens is None:
        if keep is not None:
            raise ValueError(f"{COMPACT_KEEP_ENV} is set but {COMPACT_TOKENS_ENV} is not")
        return None
    return tokens, DEFAULT_COMPACT_KEEP if keep is None else keep
