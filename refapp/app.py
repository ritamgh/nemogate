"""RefApp: the reference coding team seen through replay.app.App (what the runner needs)."""

from collections.abc import Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware

from common.env import compaction_from_env
from refapp import sandbox
from refapp.tasks import Task, load_tasks
from refapp.team import build_team


class RefApp:
    @property
    def repo_root(self) -> str:
        return sandbox.REPO

    def tasks(self) -> Sequence[Task]:
        return load_tasks()

    async def base_image(self, client: Any) -> str:
        return await sandbox.seed_image(client)

    def build(
        self,
        backend: Any,
        *,
        middleware: Sequence[AgentMiddleware],
        subagent_middleware: Sequence[AgentMiddleware],
        checkpointer: Any,
    ) -> Any:
        return build_team(
            backend,
            middleware=middleware,
            subagent_middleware=subagent_middleware,
            checkpointer=checkpointer,
            compaction=compaction_from_env(),
        )

    async def task_success(self, session: Any, task: Task) -> tuple[bool, str]:
        return await sandbox.task_success(session, task.check.name, task.check.read_bytes())


APP = RefApp()
