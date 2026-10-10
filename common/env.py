"""Environment access: the repo-root `.env`, required variables, and the live switch.

This is the only place that reads `.env` (AGENTS.md rule 2). Values are never printed or logged.
"""

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class MissingEnvError(RuntimeError):
    """A required environment variable is unset or blank. The message names it, never a value."""


def _parse_value(raw: str) -> str:
    """A `.env` value: the text between matching quotes, else everything before ` #`."""
    text = raw.lstrip()
    if text[:1] in ("'", '"'):
        end = text.find(text[0], 1)
        if end != -1:
            return text[1:end]
    return re.split(r"\s#", raw, maxsplit=1)[0].strip()


def load_dotenv(path: Path | None = None) -> None:
    """Load `[export ]KEY=VALUE` lines from the repo-root `.env` without overriding the environment.

    A quoted value ends at its closing quote (anything after is ignored); an unquoted value ends
    at the first whitespace-then-`#`.
    """
    env_file = REPO_ROOT / ".env" if path is None else Path(path)
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
