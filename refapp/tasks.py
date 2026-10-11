"""Load reference tasks: one folder per task, data only (refapp/README.md, "Tasks")."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import TypeAdapter, ValidationError

from schemas.enums import BoundaryId, ConstraintId

TASKS_DIR = Path(__file__).resolve().parent / "tasks"

_TASK_ID = re.compile(r"T[0-9]+")
_FIELDS = {"id", "constraint", "boundary", "check", "user_message"}
_CONSTRAINT = TypeAdapter(ConstraintId)
_BOUNDARY = TypeAdapter(BoundaryId)


@dataclass(frozen=True)
class Task:
    id: str
    constraint: str
    boundary: str
    check: Path
    user_message: str
    dir: Path


def _string(data: dict, field: str, path: Path) -> str:
    value = data[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {field} must be a non-empty string, got {value!r}")
    return value


def _checked(adapter: TypeAdapter, field: str, data: dict, path: Path) -> str:
    try:
        return adapter.validate_python(data[field], strict=True)
    except ValidationError as exc:
        raise ValueError(
            f"{path}: invalid {field} {data[field]!r}: {exc.errors()[0]['msg']}"
        ) from exc


def load_task(task_dir: Path) -> Task:
    """Read `task_dir/task.yaml`; raise ValueError naming the file when it is not valid."""
    task_dir = Path(task_dir)
    path = task_dir / "task.yaml"
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    if missing := _FIELDS - data.keys():
        raise ValueError(f"{path}: missing fields {sorted(missing)}")
    if extra := data.keys() - _FIELDS:
        raise ValueError(f"{path}: unknown fields {sorted(extra)}")

    task_id = _string(data, "id", path)
    if not _TASK_ID.fullmatch(task_id):
        raise ValueError(f"{path}: id must look like T1, got {task_id!r}")
    constraint = _checked(_CONSTRAINT, "constraint", data, path)
    boundary = _checked(_BOUNDARY, "boundary", data, path)
    check = task_dir / _string(data, "check", path)
    if not check.is_file():
        raise ValueError(f"{path}: check script not found: {check}")
    return Task(
        id=task_id,
        constraint=constraint,
        boundary=boundary,
        check=check,
        user_message=_string(data, "user_message", path),
        dir=task_dir,
    )


def load_tasks(root: Path = TASKS_DIR) -> list[Task]:
    """Every `root/*/task.yaml`, sorted by task number."""
    tasks = [load_task(path.parent) for path in Path(root).glob("*/task.yaml")]
    ids = [t.id for t in tasks]
    if duplicates := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"{root}: duplicate task ids {duplicates}")
    return sorted(tasks, key=lambda t: int(t.id[1:]))
