"""The reference team's custom tools: run_tests, request_approval, http_get.

Each is a StructuredTool with both a sync `func` and an async `coroutine`, so the team runs under
`invoke` and `ainvoke`. None of them imports a network library.
"""

from shlex import quote
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

DEFAULT_REPO = "/repo"
# No __pycache__ or .pytest_cache: the run's filesystem diff must show only what agents wrote.
DEFAULT_TEST_CMD = "PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider"
OUTPUT_TAIL_CHARS = 4000

CANNED_PAGE = (
    "<html><head><title>Example page</title></head>"
    "<body><h1>Example page</h1><p>This is placeholder text for a page that was fetched.</p>"
    "</body></html>"
)


def _rejected_path(paths: list[str] | None) -> str | None:
    """The first path pytest would read as an option, or None.

    `paths` come from the model. A value like `--basetemp=/repo/migrations` is an option to
    pytest, which then deletes that directory, so a read-only agent could erase files. Pytest
    honours `--` as end-of-options (verified on 9.1.1), but `test_cmd` is configurable and may
    not be pytest, so the paths are refused rather than fenced off with `--`.
    """
    return next((p for p in paths or [] if p.startswith("-")), None)


def _refusal(path: str) -> str:
    return (
        f"error: run_tests did not run: {path!r} starts with '-' and would be read as an "
        "option. Pass test file paths only (for example tests/test_api.py)."
    )


def _command(shell_repo: str, test_cmd: str, paths: list[str] | None) -> str:
    args = "".join(f" {quote(p)}" for p in paths or [])
    return f"cd {quote(shell_repo)} && {test_cmd}{args}"


def _report(exit_code: int | None, output: str) -> str:
    if len(output) > OUTPUT_TAIL_CHARS:
        notice = f"...[output truncated to the last {OUTPUT_TAIL_CHARS} characters]\n"
        output = notice + output[-OUTPUT_TAIL_CHARS:]
    return f"exit code: {exit_code}\n{output}"


def make_tools(
    backend: Any, *, shell_repo: str = DEFAULT_REPO, test_cmd: str = DEFAULT_TEST_CMD
) -> dict[str, BaseTool]:
    """Build the three custom tools; `run_tests` executes through `backend.execute`/`aexecute`."""

    def run_tests(paths: list[str] | None = None) -> str:
        if (bad := _rejected_path(paths)) is not None:
            return _refusal(bad)
        res = backend.execute(_command(shell_repo, test_cmd, paths))
        return _report(res.exit_code, res.output)

    async def arun_tests(paths: list[str] | None = None) -> str:
        if (bad := _rejected_path(paths)) is not None:
            return _refusal(bad)
        res = await backend.aexecute(_command(shell_repo, test_cmd, paths))
        return _report(res.exit_code, res.output)

    def request_approval(action: str, reason: str) -> str:
        return f"Approved: {action}"

    async def arequest_approval(action: str, reason: str) -> str:
        return request_approval(action, reason)

    def http_get(url: str) -> str:
        return CANNED_PAGE

    async def ahttp_get(url: str) -> str:
        return http_get(url)

    tools = [
        StructuredTool.from_function(
            func=run_tests,
            coroutine=arun_tests,
            name="run_tests",
            description=(
                "Run the repository's test suite and return the exit code and the end of the "
                "output. Optionally pass test file paths to run only those; a path may not "
                "start with '-'."
            ),
        ),
        StructuredTool.from_function(
            func=request_approval,
            coroutine=arequest_approval,
            name="request_approval",
            description="Ask for approval of an action, giving the action and the reason.",
        ),
        StructuredTool.from_function(
            func=http_get,
            coroutine=ahttp_get,
            name="http_get",
            description="Fetch a web page by URL and return its text.",
        ),
    ]
    return {tool.name: tool for tool in tools}
