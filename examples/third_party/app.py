"""A small Deep Agents app that is not NemoGate's reference app: its own tools, its own rules.

The only thing it takes from NemoGate is `Guard`. `calls` records every tool run that really
happened, so a blocked call shows up as nothing in the list.
"""

from pathlib import Path

from deepagents import create_deep_agent
from deepagents.middleware.subagents import SubAgent
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from nemogate import Guard

RULES = Path(__file__).with_name("rules.yaml")


def build_agent(model, rules_path=RULES, calls=None):
    calls = [] if calls is None else calls
    guard = Guard.from_yaml(rules_path)

    @tool
    def send_payment(amount: float, to: str) -> str:
        """Send a payment of `amount` to the person `to`."""
        calls.append((amount, to))
        return f"sent {amount} to {to}"

    @tool
    def delete_record(path: str) -> str:
        """Delete the record stored at `path`."""
        calls.append(path)
        return f"deleted {path}"

    return create_deep_agent(
        model=model,
        tools=[send_payment, delete_record],
        middleware=guard.middleware(),  # the main agent
        subagents=[
            SubAgent(  # the default general-purpose subagent does not get the main list
                name="general-purpose",
                description="General-purpose agent for multi-step work.",
                system_prompt="Do the work you are given.",
                model=model,
                middleware=guard.middleware(),
            )
        ],
        checkpointer=InMemorySaver(),
    )
