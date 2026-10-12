"""Action oracle: deterministic predicates over a trace and a filesystem diff (SPEC C3)."""

from oracle.evaluate import evaluate
from oracle.paths import glob_matches, repo_relative

__all__ = ["evaluate", "glob_matches", "repo_relative"]
