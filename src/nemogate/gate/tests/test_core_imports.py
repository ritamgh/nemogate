"""A core install (no `proof` extra) imports `nemogate` and `nemogate.Guard`."""

import subprocess
import sys

SCRIPT = """
import sys
import nemogate
from nemogate import Guard
heavy = [
    name for name in sys.modules
    if name.split(".")[0] in {"contree", "contree_sdk", "aiosqlite"}
    or name.startswith(("langgraph.checkpoint.sqlite", "nemogate.proof"))
]
print(sorted(heavy))
"""


def test_importing_the_guard_loads_neither_the_proof_package_nor_its_extra():
    # Breaks if nemogate/__init__ or nemogate.gate (or anything they import) pulls in
    # nemogate.proof, contree_sdk or the sqlite checkpointer: a core install would fail on import.
    # A fresh interpreter, so modules other tests loaded do not hide it; -I keeps the checkout's
    # current directory off sys.path.
    done = subprocess.run(
        [sys.executable, "-I", "-c", SCRIPT], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"
