"""A step bound for every agent in a run, planner and subagents alike.

`recursion_limit` in the config the runner passes the graph bounds only the planner graph. Deep
Agents compiles each subagent with a `recursion_limit` of 9999 of its own, and that one wins over
the parent's (docs/FACTS.md, "Subagent step bound"), so a subagent that loops (a stub or a model
that keeps writing the same file) runs until a human kills the process. `StepCap` is a callback
handler the runner puts in the config's `callbacks`: they reach the subagent graphs and it sees
each chat model call before it is sent, with the superstep of the graph that call runs in. Past
the limit it raises, the run ends as an errored run with an `error.json` (the path a
GraphRecursionError takes) and the batch carries on.

It needs nothing from the app: no subagent middleware, no change to how a subagent is built.
"""

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphRecursionError

STEP_KEY = "langgraph_step"  # in the metadata of every call a graph node makes


class StepLimitReached(GraphRecursionError):
    """An agent's graph went past the step limit; subclasses GraphRecursionError so anything that
    treats a recursion error as an errored run treats this one the same way."""


class StepCap(BaseCallbackHandler):
    """Raises StepLimitReached on the model call that would start a superstep past `limit`.

    Supersteps are counted per graph, as `recursion_limit` counts them: a model call and its tool
    calls take two. A graph's first model call is at step 2, so a limit of N allows N // 2 calls.
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
        step = (kwargs.get("metadata") or {}).get(STEP_KEY)
        if isinstance(step, int) and step > self.limit:
            raise StepLimitReached(
                f"Step limit of {self.limit} reached by an agent: its model call at step "
                f"{step} was not made. The run is ended and recorded as an error."
            )
