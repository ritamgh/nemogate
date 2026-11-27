"""NemoGate: a rule guard for Deep Agents, plus the ledger, oracle and schemas it runs on."""

from importlib.metadata import PackageNotFoundError, version

from nemogate.gate import Guard, GuardLoopError

try:
    __version__ = version("nemogate")
except PackageNotFoundError:  # run from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["Guard", "GuardLoopError", "__version__"]
