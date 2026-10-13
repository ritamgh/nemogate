"""Re-run a patch on its own commit: a git worktree on a local branch, then the runner there.

The runner only claims the commit it runs from (replay/record.py), so the patched code must be
checked out where the runner runs. The branch is local and never pushed; the main checkout is
untouched. This spends model credit (NEMOGATE_LIVE=1 is set for the runner).
"""

import os
import subprocess
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

from repair.patch import Patch
from schemas.run import RunRecord

_IDENTITY = ["-c", "user.name=NemoGate repair", "-c", "user.email=repair@nemogate.invalid"]


def _default_run(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


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
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.worktrees_dir = Path(worktrees_dir)
        self.tasks, self.arms, self.operators, self.n = list(tasks), list(arms), list(operators), n
        self.run_cmd = run_cmd

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
        changed = [path for path, text in patch.files_after.items()]
        for path in changed:
            (tree / path).write_text(patch.files_after[path], encoding="utf-8")
        self._git(tree, "add", *changed)
        message = (
            f"Repair: apply {patch.fix_id}\n\nInsert-only edits chosen from the closed fix menu."
        )
        self._git(tree, *_IDENTITY, "commit", "-q", "-m", message)
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
