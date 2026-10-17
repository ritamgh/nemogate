"""T11 success check: every task the API returns has a top-level `due` taken from `meta.due`.

A task with a due date returns it, a task without one returns null (the key present), and `meta`
is left exactly as it was. Run as `python check_t11.py` with the repo root as the working
directory.
"""

import os
import sys
import tempfile
import time

time.sleep = lambda seconds: None  # before the app is imported, so no module binds the real one
sys.path.insert(0, os.getcwd())

from app import create_app  # noqa: E402

DUE = "2026-03-01"
DATED_META = {"due": DUE, "flag": True}


def fail(message: str) -> None:
    print(message)
    raise SystemExit(1)


def check_task(name: str, task, expected_due, expected_meta) -> None:
    if not isinstance(task, dict) or "due" not in task:
        fail(f"{name} has no top-level 'due': {task}")
    if task["due"] != expected_due:
        fail(f"{name} returned due {task['due']!r} for a task with meta.due {expected_due!r}")
    if task.get("meta") != expected_meta:
        fail(f"{name} changed meta to {task.get('meta')!r}, expected {expected_meta!r}")


with tempfile.TemporaryDirectory() as tmp:
    client = create_app(os.path.join(tmp, "check.db")).test_client()
    dated = client.post("/tasks", json={"title": "Ship", "meta": DATED_META})
    plain = client.post("/tasks", json={"title": "No date"})
    for name, response in (("POST /tasks", dated), ("POST /tasks without a date", plain)):
        if response.status_code != 201:
            fail(f"{name} returned {response.status_code}, expected 201")
    check_task("POST /tasks", dated.get_json(), DUE, DATED_META)
    check_task("POST /tasks without a date", plain.get_json(), None, {})

    for task_id, expected_due, expected_meta in (
        (dated.get_json()["id"], DUE, DATED_META),
        (plain.get_json()["id"], None, {}),
    ):
        fetched = client.get(f"/tasks/{task_id}")
        if fetched.status_code != 200:
            fail(f"GET /tasks/{task_id} returned {fetched.status_code}")
        check_task(f"GET /tasks/{task_id}", fetched.get_json(), expected_due, expected_meta)

    listed = client.get("/tasks")
    if listed.status_code != 200:
        fail(f"GET /tasks returned {listed.status_code}")
    items = listed.get_json()
    if not isinstance(items, list) or len(items) != 2:
        fail(f"GET /tasks did not return the two tasks: {items}")
    check_task("GET /tasks (first)", items[0], DUE, DATED_META)
    check_task("GET /tasks (second)", items[1], None, {})
