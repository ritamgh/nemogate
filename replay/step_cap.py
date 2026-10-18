"""A step bound for every subagent in a run; the planner keeps its own `recursion_limit`.

`recursion_limit` in the config the runner passes the graph bounds only the planner graph. Deep
Agents compiles each subagent with a `recursion_limit` of 9999 of its own, and that one wins over
the parent's (docs/FACTS.md, "Subagent step bound"), so a subagent that loops (a stub or a model
that keeps writing the same file) runs until a human kills the process. `StepCap` is a callback
handler the runner puts in the config's `callbacks`: they reach the subagent graphs and it sees
each chat model call before it is sent, with the superstep of the graph that call runs in. Past
the limit it raises, the run ends as an errored run with an `error.json` (the path a
GraphRecursionError takes) and the batch carries on.

The planner's own graph is not counted. A resumed graph (a fork) starts at its checkpoint's
superstep + 1 (`langgraph/pregel/_loop.py:1769`), so the planner's absolute step is the source
run's steps plus the fork's, and a `recursion_limit` raised or a long source would make a cut at
`limit` look like a runaway subagent. The planner has a working bound already: `recursion_limit`
is relative to where the graph resumed. A subagent graph always starts fresh, so its step is its
own. The two are told apart by `langgraph_checkpoint_ns`: the planner's is `model:<id>`, a
subagent's has one `tools:<id>|` segment per graph above it.

It needs nothing from the app: no subagent middleware, no change to how a subagent is built.
"""

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphRecursionError

STEP_KEY = "langgraph_step"  # in the metadata of every call a graph node makes
NS_KEY = "langgraph_checkpoint_ns"  # likewise; "|" separates the graphs a call is nested in


class StepLimitReached(GraphRecursionError):
    """An agent's graph went past the step limit; subclasses GraphRecursionError so anything that
    treats a recursion error as an errored run treats this one the same way."""


class StepCap(BaseCallbackHandler):
    """Raises StepLimitReached on a subagent's model call that would start a superstep past `limit`.

    Supersteps are counted per graph, as `recursion_limit` counts them: a model call and its tool
    calls take two. A subagent's first model call is at step 2, so a limit of N allows N // 2
    calls. The planner's graph is skipped (module docstring).
    """

    # LangChain logs and swallows an exception from a handler unless it is told to raise.
    raise_error = True

    def __init__(self, limit: int) -> None:
        if limit < 2:
            raise ValueError(f"step limit must be at least 2, got {limit}")
        self.limit = limit

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: Any, *args: Any, **kwargs: Any
    ) -> None:
        metadata = kwargs.get("metadata") or {}
        step = metadata.get(STEP_KEY)
        in_subagent = "|" in (metadata.get(NS_KEY) or "")
        if in_subagent and isinstance(step, int) and step > self.limit:
            raise StepLimitReached(
                f"Step limit of {self.limit} reached by a subagent: its model call at step "
                f"{step} was not made. The run is ended and recorded as an error."
            )
