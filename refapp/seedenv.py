"""Run the seed repo outside the sandbox, in an isolated Python env.

The nemogate env has no Flask and must not get it. Seed code runs in an env built from the
SEED's requirements.txt, never an overlay's, so a dependency an overlay adds is not installed
(in the sandbox the agents have no shell to install one either).
"""

import shutil
import subprocess
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent / "seed"
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
_TAIL_CHARS = 2000
_TIMEOUT_RETURNCODE = 124  # the exit code of coreutils `timeout`


def materialize(dest: Path, overlay: Path | None = None) -> Path:
    """Copy the seed into `dest`, then apply an overlay on top; return `dest`.

    Overlay layout: every file under `overlay/files/` is written over `dest` at the same
    relative path, then every non-blank line of `overlay/delete.txt` is a relative path that is
    removed. A delete line that escapes `dest` or names a missing path is an error.
    """
    dest = Path(dest)
    shutil.copytree(SEED_DIR, dest, dirs_exist_ok=True, ignore=_COPY_IGNORE)
    if overlay is None:
        return dest
    files = overlay / "files"
    if files.is_dir():
        shutil.copytree(files, dest, dirs_exist_ok=True)
    delete_list = overlay / "delete.txt"
    if delete_list.is_file():
        root = dest.resolve()
        for line in delete_list.read_text().splitlines():
            if not line.strip():
                continue
            target = (root / line.strip()).resolve()
            if not target.is_relative_to(root) or target == root:
                raise ValueError(f"delete.txt path escapes the repo: {line.strip()!r}")
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
    return dest


def run_seed_python(
    repo: Path, args: list[str], timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    """Run `python <args>` with cwd=repo in the seed's isolated env. Never raises on failure.

    A run that exceeds `timeout` comes back with returncode 124 instead of raising.
    """
    cmd = [
        "uv",
        "run",
        "--isolated",
        "--no-project",
        "--with-requirements",
        str(SEED_DIR / "requirements.txt"),
        "python",
        *args,
    ]
    try:
        return subprocess.run(
            cmd, cwd=Path(repo), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""
        err += f"\ntimed out after {timeout} s"
        return subprocess.CompletedProcess(cmd, _TIMEOUT_RETURNCODE, out, err)


def _tail(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout + proc.stderr)[-_TAIL_CHARS:]


def task_success(repo: Path, check_script: Path) -> tuple[bool, str]:
    """True when the repo's tests pass and `check_script` exits 0, both with cwd=repo.

    The str is the tail of the output of the first step that failed, or "" on success.
    """
    tests = run_seed_python(repo, ["-m", "pytest", "-q"])
    if tests.returncode != 0:
        return False, _tail(tests)
    check = run_seed_python(repo, [str(Path(check_script).resolve())])
    if check.returncode != 0:
        return False, _tail(check)
    return True, ""
