"""Pre-call blocking for the block_only and gated arms, decided by the oracle itself.

A call is blocked when evaluating the trace so far plus this call, as if it ran, gives a
violation at this call's step. Only checks SPEC marks as checkable before the call take part,
so the gate and the oracle can never disagree about a blocked call.
"""

from oracle import evaluate
from schemas.ledger import ApprovalBefore, Constraint, Ledger, PathForbidden, ToolForbidden
from schemas.trace import Event, ToolCall

_BEFORE_THE_CALL = (PathForbidden, ToolForbidden, ApprovalBefore)


def checkable_before_the_call(constraint: Constraint) -> bool:
    return isinstance(constraint.check, _BEFORE_THE_CALL)


def blocking_constraint(ledger: Ledger, prior: list[Event], call: ToolCall) -> Constraint | None:
    """The first constraint `call` would break if it ran after `prior`, or None."""
    for constraint in ledger.constraints:
        if not checkable_before_the_call(constraint):
            continue
        single = ledger.model_copy(update={"constraints": (constraint,)})
        if any(v.step == call.step for v in evaluate(single, [*prior, call], [])):
            return constraint
    return None
