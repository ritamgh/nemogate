"""build_team(): the reference coding team (planner delegating to coder and reviewer).

The gate is not attached here. `middleware` and `subagent_middleware` are pass-through stacks:
the first goes on the planner (delegation and return boundaries), the second on coder and
reviewer (their inner tool calls).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel

from common.llm import get_model
from refapp.prompts import (
    CODER_DESCRIPTION,
    CODER_PROMPT,
    PLANNER_PROMPT,
    REVIEWER_DESCRIPTION,
    REVIEWER_PROMPT,
)
from refapp.tools import make_tools
from schemas.enums import Role

REPO = "/repo"
AGENTS = ("planner", "coder", "reviewer")

# The built-in file tools each agent gets. `execute` is in none of them, and only the coder can
# write or delete, so every change crosses the planner's delegation boundary.
READ_TOOLS = ["ls", "read_file", "glob", "grep"]
CODER_FILE_TOOLS = ["ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep"]


def _disable_general_purpose(planner_model: BaseChatModel) -> None:
    """Stop Deep Agents adding its `general-purpose` subagent next to coder and reviewer.

    The switch lives on a harness profile, which Deep Agents looks up in a process-wide registry
    by the model's provider (`_get_ls_params()["ls_provider"]`), so the profile is registered
    for the planner model's provider. Registering again merges, so this is idempotent.
    """
    provider = planner_model._get_ls_params().get("ls_provider")
    if not provider:
        raise ValueError(f"cannot tell the provider of {type(planner_model).__name__}")
    register_harness_profile(
        provider,
        HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
    )


def build_team(
    backend: Any,
    *,
    models: Mapping[str, BaseChatModel] | None = None,
    middleware: Sequence[AgentMiddleware] = (),
    subagent_middleware: Sequence[AgentMiddleware] = (),
    checkpointer: Any = None,
    shell_repo: str = REPO,
):
    """Build the team as a compiled Deep Agents graph (the planner is the entry point)."""
    unknown = set(models or {}) - set(AGENTS)
    if unknown:
        raise ValueError(f"unknown agents in models: {sorted(unknown)}; expected {AGENTS}")
    chosen = {name: (models or {}).get(name) or get_model(Role.sut) for name in AGENTS}
    tools = make_tools(backend, shell_repo=shell_repo)
    _disable_general_purpose(chosen["planner"])

    def fs(allowed: list[str]) -> FilesystemMiddleware:
        # Same name as the default slot, so it replaces the default (which would add `execute`
        # on a shell backend) in the planner and in each subagent.
        return FilesystemMiddleware(backend=backend, tools=allowed)

    subagents = [
        {
            "name": "coder",
            "description": CODER_DESCRIPTION,
            "system_prompt": CODER_PROMPT,
            "model": chosen["coder"],
            "tools": [tools["run_tests"], tools["request_approval"], tools["http_get"]],
            "middleware": [fs(CODER_FILE_TOOLS), *subagent_middleware],
        },
        {
            "name": "reviewer",
            "description": REVIEWER_DESCRIPTION,
            "system_prompt": REVIEWER_PROMPT,
            "model": chosen["reviewer"],
            "tools": [tools["run_tests"]],
            "middleware": [fs(READ_TOOLS), *subagent_middleware],
        },
    ]
    return create_deep_agent(
        model=chosen["planner"],
        tools=[tools["run_tests"], tools["request_approval"], tools["http_get"]],
        system_prompt=PLANNER_PROMPT,
        subagents=subagents,
        middleware=[fs(READ_TOOLS), *middleware],
        backend=backend,
        checkpointer=checkpointer,
    )
