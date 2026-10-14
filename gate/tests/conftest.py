import os
import sys
from pathlib import Path

import pytest
from deepagents.backends import LocalShellBackend


@pytest.fixture
def backend(tmp_path: Path) -> LocalShellBackend:
    """A local backend whose /repo is tmp_path/repo, with app/ and migrations/ folders."""
    (tmp_path / "repo" / "app").mkdir(parents=True)
    (tmp_path / "repo" / "migrations").mkdir()
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    return LocalShellBackend(
        root_dir=tmp_path, virtual_mode=True, inherit_env=True, env={"PATH": path}
    )
