"""Re-run a patch on its own commit: a git worktree on a local branch, then the runner there.

The runner only claims the commit it runs from (replay/record.py), so the patched code must be
checked out where the runner runs. The branch is local and never pushed; the main checkout is
untouched. This spends model credit (NEMOGATE_LIVE=1 is set for the runner); callers must check
the budget first. A rejected patch's worktree and branch are removed with discard_last(); a round
that did not finish keeps them, because the re-runs that did finish are records in the worktree
(`completed()`).
"""

import os
import signal
import subprocess
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

from repair.patch import Patch
from schemas.run import RunRecord

_IDENTITY = ["-c", "user.name=NemoGate repair", "-c", "user.email=repair@nemogate.invalid"]


STOP_GRACE_SECONDS = 20  # how long a stopped runner gets to wind down before it is killed


def _default_run(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    """Run `cmd` to the end, like `subprocess.run(check=True)`, but if this process is stopped
    first (Ctrl-C, or a SIGTERM turned into KeyboardInterrupt), ask the runner to stop and wait for
    it. `subprocess.run` sends SIGKILL, which `uv run` does not pass on, and the runner behind it
    would go on making model calls after the round was reported as stopped. The runner stays in
    this process's group, so a kill of the whole group still reaches it."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env)
    try:
        code = proc.wait()
    except BaseException:
        _stop(proc)
        raise
    if code != 0:
        raise subprocess.CalledProcessError(code, cmd)


def _descendants(pid: int) -> list[int]:
    """Every process below `pid`, deepest first (so killing in order never orphans one unseen)."""
    table = subprocess.run(
        ["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, check=False
    ).stdout
    children: dict[int, list[int]] = {}
    for line in table.splitlines():
        child, parent = (int(field) for field in line.split())
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    pending = [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found[::-1]  # a parent is always found before its children, so reversed is deepest first


def _kill_tree(proc: "subprocess.Popen[bytes]", seen: list[int]) -> None:
    """SIGKILL what is below `proc`, then `proc`. `uv run` does not pass a kill on, so killing only
    `proc` leaves the runner behind it alive. `seen` is what was below it before it was asked to
    stop: a wrapper that exits first re-parents its child out of sight."""
    below = _descendants(proc.pid)
    for pid in [*below, *(p for p in seen if p not in below)]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.kill()


def _stop(proc: "subprocess.Popen[bytes]") -> None:
    """Ask `proc` to stop, wait for it, and kill it and everything below it if it does not (or if
    the user presses Ctrl-C again while waiting: they want it gone now, and an orphaned runner
    keeps spending credit). The request goes to `proc` only: `uv run` passes it on, and sending it
    to the runner as well would deliver it twice."""
    seen = _descendants(proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=STOP_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        _kill_tree(proc, seen)
        proc.wait()


class WorktreeRerun:
    def __init__(
        self,
        *,
        repo_dir: str | Path,
        worktrees_dir: str | Path,
        tasks: Sequence[str],
        arms: Sequence[str],
        operators: Sequence[str],
        n: int,
        run_cmd: Callable[[list[str], Path, dict[str, str]], None] = _default_run,
        on_start: Callable[[Path, str], None] | None = None,
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.worktrees_dir = Path(worktrees_dir)
        self.tasks, self.arms, self.operators, self.n = list(tasks), list(arms), list(operators), n
        self.run_cmd = run_cmd
        self.on_start = on_start  # called with (worktree, branch) once the patch is committed
        self.last: tuple[Path, str] | None = None  # (worktree, branch) of the latest re-run

    def completed(self) -> list[RunRecord]:
        """The re-run records the latest worktree holds so far, for a round that was stopped.

        A record that cannot be read (the run in progress when the runner was stopped) is left
        out rather than failing the report.
        """
        if self.last is None:
            return []
        records = []
        for path in sorted((self.last[0] / "runs").glob("*/record.json")):
            try:
                records.append(RunRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return records

    def discard_last(self) -> None:
        """Remove the latest re-run's worktree and its local branch (for a rejected patch)."""
        if self.last is None:
            return
        tree, branch = self.last
        self._git(self.repo_dir, "worktree", "remove", "--force", str(tree))
        self._git(self.repo_dir, "branch", "-D", branch)
        self.last = None

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

    def __call__(self, patch: Patch) -> list[RunRecord]:
        for path in patch.files_after:
            parts = PurePosixPath(path.replace("\\", "/"))
            if parts.is_absolute() or ".." in parts.parts:
                raise ValueError(f"patched files must be repo-relative paths inside it: {path!r}")
        branch = f"repair/{patch.fix_id}-{uuid.uuid4().hex[:6]}"
        tree = self.worktrees_dir / branch.replace("/", "-")
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._git(self.repo_dir, "worktree", "add", "-q", "-b", branch, str(tree), "HEAD")
        self.last = (tree, branch)
        changed = list(patch.files_after)
        for path in changed:
            (tree / path).write_text(patch.files_after[path], encoding="utf-8")
        self._git(tree, "add", *changed)
        message = (
            f"Repair: apply {patch.fix_id}\n\nInsert-only edits chosen from the closed fix menu."
        )
        self._git(tree, *_IDENTITY, "commit", "-q", "-m", message)
        if self.on_start is not None:
            self.on_start(tree, branch)
        # --yes: the repair CLI has already checked the run count against the repair bucket
        # and required its own --yes (repair/__main__.py), so the inner runner does not ask.
        cmd = [
            "uv", "run", "python", "-m", "replay.runner",
            "--tasks", *self.tasks, "--arms", *self.arms, "--operators", *self.operators,
            "--n", str(self.n), "--repo-ref", "HEAD", "--yes",
        ]  # fmt: skip
        self.run_cmd(cmd, tree, {**os.environ, "NEMOGATE_LIVE": "1"})
        return [
            RunRecord.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted((tree / "runs").glob("*/record.json"))
        ]
