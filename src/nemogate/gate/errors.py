"""Errors the rule guard raises."""


class GuardLoopError(RuntimeError):
    """The same call was blocked by one rule too many times in a thread; the run must stop."""

    def __init__(self, rule_id: str, tool: str, thread_id: str) -> None:
        super().__init__(
            f"rule {rule_id!r} blocked the same {tool!r} call again and again in thread "
            f"{thread_id!r}; stopping instead of looping"
        )
        self.rule_id = rule_id
        self.tool = tool
        self.thread_id = thread_id
