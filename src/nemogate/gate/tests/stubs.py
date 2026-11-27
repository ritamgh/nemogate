"""Scripted helper replies for the operators' structured output (tests only)."""

from langchain_core.messages import AIMessage


def picks(*numbers: int) -> AIMessage:
    """The helper naming the sentences (1-based) that carry the rule (what drop removes)."""
    return AIMessage(
        content="", tool_calls=[{"name": "Picks", "id": "p", "args": {"numbers": list(numbers)}}]
    )


def rewrites(*pairs: tuple[int, str]) -> AIMessage:
    """The helper giving (sentence number, replacement) pairs (what hedge swaps in)."""
    items = [{"number": n, "replacement": r} for n, r in pairs]
    return AIMessage(
        content="", tool_calls=[{"name": "Rewrites", "id": "r", "args": {"rewrites": items}}]
    )
