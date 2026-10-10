"""build_team(): the reference coding team (planner delegating to coder and reviewer).

The gate is not attached here. `middleware` and `subagent_middleware` are pass-through stacks:
the first goes on the planner (delegation and return boundaries), the second on coder and
reviewer (their inner tool calls).
"""

import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.profiles.harness.harness_profiles import (
    _HARNESS_PROFILES,
    _ensure_harness_profiles_loaded,
)
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
from refapp.tools import DEFAULT_TEST_CMD, make_tools
from schemas.enums import Role

REPO = "/repo"
AGENTS = ("planner", "coder", "reviewer")

# The built-in file tools each agent gets. `execute` is in none of them, and only the coder can
# write or delete, so every change crosses the planner's delegation boundary.
READ_TOOLS = ["ls", "read_file", "glob", "grep"]
CODER_FILE_TOOLS = ["ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep"]


_PROFILE_LOCK = threading.Lock()


@contextmanager
def _general_purpose_disabled(planner_model: BaseChatModel) -> Iterator[None]:
    """Stop Deep Agents adding its `general-purpose` subagent next to coder and reviewer, only
    while the body runs.

    The switch lives on a harness profile, which Deep Agents looks up in a process-wide registry
    by the model's provider (`_get_ls_params()["ls_provider"]`), so the profile is registered
    for the planner model's provider. `create_deep_agent` reads it while it assembles the graph
    (graph.py:631 and :822) and never again, so the registry entry is put back exactly as it was
    (the previous profile, or absent) when the body ends, even on an error. Otherwise every later
    deep agent on that provider would lose its general-purpose subagent. The lock keeps two
    builds in different threads from restoring each other's state.
    """
    provider = planner_model._get_ls_params().get("ls_provider")
    if not provider:
        raise ValueError(f"cannot tell the provider of {type(planner_model).__name__}")
    with _PROFILE_LOCK:
        # No public API removes a registration, and a registration merges with the previous one,
        # so the private registry (harness_profiles.py:940) is read and restored directly.
        # The built-in profiles load lazily on first use; load them first so "absent" below
        # means absent after the bootstrap, not before it.
        _ensure_harness_profiles_loaded()
        previous = _HARNESS_PROFILES.get(provider)
        register_harness_profile(
            provider,
            HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
        )
        try:
            yield
        finally:
            if previous is None:
                _HARNESS_PROFILES.pop(provider, None)
            else:
                _HARNESS_PROFILES[provider] = previous


def build_team(
    backend: Any,
    *,
    models: Mapping[str, BaseChatModel] | None = None,
    middleware: Sequence[AgentMiddleware] = (),
    subagent_middleware: Sequence[AgentMiddleware] = (),
    checkpointer: Any = None,
    shell_repo: str = REPO,
    test_cmd: str = DEFAULT_TEST_CMD,
):
    """Build the team as a compiled Deep Agents graph (the planner is the entry point)."""
    unknown = set(models or {}) - set(AGENTS)
    if unknown:
        raise ValueError(f"unknown agents in models: {sorted(unknown)}; expected {AGENTS}")
    chosen = {name: (models or {}).get(name) or get_model(Role.sut) for name in AGENTS}
    tools = make_tools(backend, shell_repo=shell_repo, test_cmd=test_cmd)

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
    with _general_purpose_disabled(chosen["planner"]):
        return create_deep_agent(
            model=chosen["planner"],
            tools=[tools["run_tests"], tools["request_approval"], tools["http_get"]],
            system_prompt=PLANNER_PROMPT,
            subagents=subagents,
            middleware=[fs(READ_TOOLS), *middleware],
            backend=backend,
            checkpointer=checkpointer,
        )
