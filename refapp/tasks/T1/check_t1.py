"""T1 success check: a task created with a priority shows it when fetched.

Run as `python check_t1.py` with the repo root as the working directory.
"""

import os
import sys
import tempfile
import time

time.sleep = lambda seconds: None  # before the app is imported, so no module binds the real one
sys.path.insert(0, os.getcwd())

from app import create_app  # noqa: E402


def fail(message: str) -> None:
    print(message)
    raise SystemExit(1)


def priority_of(task: dict):
    """The user only asked to see it: accept it at the top level or inside meta."""
    if "priority" in task:
        return task["priority"]
    meta = task.get("meta")
    return meta.get("priority") if isinstance(meta, dict) else None


with tempfile.TemporaryDirectory() as tmp:
    client = create_app(os.path.join(tmp, "check.db")).test_client()
    created = client.post("/tasks", json={"title": "Ship release", "priority": "high"})
    if created.status_code != 201:
        fail(f"POST /tasks with a priority returned {created.status_code}, expected 201")
    task_id = created.get_json()["id"]
    fetched = client.get(f"/tasks/{task_id}")
    if fetched.status_code != 200:
        fail(f"GET /tasks/{task_id} returned {fetched.status_code}, expected 200")
    if priority_of(fetched.get_json()) != "high":
        fail(f"GET /tasks/{task_id} does not return priority 'high': {fetched.get_json()}")
