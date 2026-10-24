"""ResearchApp: the research team seen through replay.app.App (what the runner needs).

Select it with `NEMOGATE_APP=refapp.research.app:APP` (replay/README.md, "Choosing the app").
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel

from common.env import COMPACT_TOKENS_ENV, compaction_from_env
from refapp.research import sandbox
from refapp.research.search import CachedSearch, make_search_tool, tavily_client
from refapp.research.team import build_team
from refapp.tasks import Task, load_tasks

TASKS_DIR = Path(__file__).resolve().parent / "tasks"


class ResearchApp:
    """`models` and `searcher` are for tests: stub models per agent, a search over a fake client.
    The default searcher is the Tavily client behind the read-through cache, built on the first
    cache miss of a live run."""

    def __init__(
        self,
        *,
        models: Mapping[str, BaseChatModel] | None = None,
        searcher: CachedSearch | None = None,
    ) -> None:
        self._models = models
        self._searcher = searcher or CachedSearch(tavily_client)

    @property
    def repo_root(self) -> str:
        return sandbox.REPO

    def tasks(self) -> Sequence[Task]:
        return load_tasks(TASKS_DIR)

    async def base_image(self, client: Any) -> str:
        return await sandbox.research_image(client)

    def build(
        self,
        backend: Any,
        *,
        middleware: Sequence[AgentMiddleware],
        subagent_middleware: Sequence[AgentMiddleware],
        checkpointer: Any,
    ) -> Any:
        if compaction_from_env() is not None:
            # The runner records the knob in each run's carriage; a team that ignored it would be
            # recorded under a setting it never had.
            raise ValueError(
                f"the research app has no planner-compaction support: unset {COMPACT_TOKENS_ENV}"
            )
        return build_team(
            backend,
            search_tool=make_search_tool(self._searcher),
            models=self._models,
            middleware=middleware,
            subagent_middleware=subagent_middleware,
            checkpointer=checkpointer,
        )

    async def task_success(self, session: Any, task: Task) -> tuple[bool, str]:
        return await sandbox.task_success(session, task.check.name, task.check.read_bytes())


APP = ResearchApp()
