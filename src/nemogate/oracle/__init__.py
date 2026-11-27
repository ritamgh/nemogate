"""Action oracle: deterministic predicates over a trace and a filesystem diff (SPEC C3)."""

from nemogate.oracle.evaluate import arg_bound_problem, evaluate
from nemogate.oracle.paths import glob_matches, repo_relative

__all__ = ["arg_bound_problem", "evaluate", "glob_matches", "repo_relative"]
