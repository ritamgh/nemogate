"""The fork and replay engine that proves a rule was lost; it needs `pip install nemogate[proof]`.

Nothing outside this package imports it, so a core install of `nemogate` never reaches the extra.
"""

import importlib
from types import ModuleType


def import_extra(name: str) -> ModuleType:
    """Import `name`, which only the `proof` extra installs, or say how to get it."""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ImportError(
            f"{name} is not installed, and the proof engine needs it: "
            "run `pip install nemogate[proof]`"
        ) from exc
