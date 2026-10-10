"""Environment access: the repo-root `.env`, required variables, and the live switch.

This is the only place that reads `.env` (AGENTS.md rule 2). Values are never printed or logged.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class MissingEnvError(RuntimeError):
    """A required environment variable is unset or blank. The message names it, never a value."""


def load_dotenv(path: Path | None = None) -> None:
    """Load `KEY=VALUE` lines from the repo-root `.env` without overriding the environment."""
    env_file = REPO_ROOT / ".env" if path is None else Path(path)
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value


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
