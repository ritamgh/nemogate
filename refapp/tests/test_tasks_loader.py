from pathlib import Path

import pytest
import yaml

from refapp.tasks import TASKS_DIR, Task, load_task, load_tasks

GOOD = {
    "id": "T1",
    "constraint": "C-001",
    "boundary": "delegation:planner->coder",
    "check": "check_t1.py",
    "user_message": "Please add a thing.\nThanks.",
}


def write_task(root: Path, folder: str, **overrides) -> Path:
    """A task folder under `root` with a valid task.yaml, a check script and the overrides."""
    task_dir = root / folder
    task_dir.mkdir(parents=True)
    fields = {**GOOD, **overrides}
    fields = {k: v for k, v in fields.items() if v is not None}
    (task_dir / "task.yaml").write_text(yaml.safe_dump(fields))
    (task_dir / "check_t1.py").write_text("raise SystemExit(0)\n")
    return task_dir


def test_load_task_reads_every_field(tmp_path):
    task_dir = write_task(tmp_path, "T1")

    task = load_task(task_dir)

    assert task == Task(
        id="T1",
        constraint="C-001",
        boundary="delegation:planner->coder",
        check=task_dir / "check_t1.py",
        user_message="Please add a thing.\nThanks.",
        dir=task_dir,
    )


def test_load_task_keeps_folded_user_message_text(tmp_path):
    task_dir = tmp_path / "T2"
    task_dir.mkdir()
    (task_dir / "check_t2.py").write_text("")
    (task_dir / "task.yaml").write_text(
        "id: T2\n"
        "constraint: C-002\n"
        "boundary: 'return:coder->planner'\n"
        "check: check_t2.py\n"
        "user_message: >-\n"
        "  Hey, can you do a thing?\n"
        "  Heads up, it matters.\n"
    )

    assert load_task(task_dir).user_message == "Hey, can you do a thing? Heads up, it matters."


@pytest.mark.parametrize("task_id", ["t1", "T", "T1a", "C-001", "T-1", "1", "T1\n"])
def test_load_task_rejects_bad_ids(tmp_path, task_id):
    task_dir = write_task(tmp_path, "T1", id=task_id)

    with pytest.raises(ValueError, match="id must look like T1"):
        load_task(task_dir)


@pytest.mark.parametrize("constraint", ["C-01", "c-001", "T1", "C001", "C-001x"])
def test_load_task_rejects_bad_constraint_ids(tmp_path, constraint):
    task_dir = write_task(tmp_path, "T1", constraint=constraint)

    with pytest.raises(ValueError, match="constraint"):
        load_task(task_dir)


@pytest.mark.parametrize(
    "boundary",
    ["planner->coder", "delegation:planner", "handoff:planner->coder", "delegation:a->b->c"],
)
def test_load_task_rejects_bad_boundaries(tmp_path, boundary):
    task_dir = write_task(tmp_path, "T1", boundary=boundary)

    with pytest.raises(ValueError, match="boundary"):
        load_task(task_dir)


def test_load_task_accepts_compaction_boundary(tmp_path):
    task_dir = write_task(tmp_path, "T1", boundary="compaction:planner")

    assert load_task(task_dir).boundary == "compaction:planner"


def test_load_task_rejects_non_string_constraint(tmp_path):
    task_dir = write_task(tmp_path, "T1", constraint=1)

    with pytest.raises(ValueError, match="constraint"):
        load_task(task_dir)


def test_load_task_rejects_missing_check_file(tmp_path):
    task_dir = write_task(tmp_path, "T1", check="check_t9.py")

    with pytest.raises(ValueError, match="check script not found"):
        load_task(task_dir)


def test_load_task_rejects_missing_and_unknown_fields(tmp_path):
    no_message = write_task(tmp_path, "T1", user_message=None)
    extra = write_task(tmp_path, "T2", id="T2", hint="be careful")

    with pytest.raises(ValueError, match=r"missing fields \['user_message'\]"):
        load_task(no_message)
    with pytest.raises(ValueError, match=r"unknown fields \['hint'\]"):
        load_task(extra)


def test_load_task_rejects_blank_user_message(tmp_path):
    task_dir = write_task(tmp_path, "T1", user_message="  \n")

    with pytest.raises(ValueError, match="user_message must be a non-empty string"):
        load_task(task_dir)


def test_load_task_rejects_non_mapping_yaml(tmp_path):
    task_dir = tmp_path / "T1"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text("- just\n- a list\n")

    with pytest.raises(ValueError, match="expected a mapping"):
        load_task(task_dir)


def test_load_tasks_sorts_by_task_number_not_text(tmp_path):
    write_task(tmp_path, "T10", id="T10")
    write_task(tmp_path, "T2", id="T2")
    write_task(tmp_path, "T1", id="T1")

    assert [t.id for t in load_tasks(tmp_path)] == ["T1", "T2", "T10"]


def test_load_tasks_ignores_folders_without_task_yaml(tmp_path):
    write_task(tmp_path, "T1")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "notes.txt").write_text("x")

    assert [t.id for t in load_tasks(tmp_path)] == ["T1"]


def test_load_tasks_rejects_duplicate_ids(tmp_path):
    write_task(tmp_path, "T1")
    write_task(tmp_path, "T1-copy")

    with pytest.raises(ValueError, match=r"duplicate task ids \['T1'\]"):
        load_tasks(tmp_path)


def test_load_tasks_default_root_is_the_tasks_folder():
    assert TASKS_DIR.name == "tasks"
    assert TASKS_DIR.parent.name == "refapp"
    # Whatever is committed there must load.
    assert all(isinstance(t, Task) for t in load_tasks())
