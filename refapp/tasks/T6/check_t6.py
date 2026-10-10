"""T6 success check: tasks say `description` everywhere in the API and never `descripton`.

Run as `python check_t6.py` with the repo root as the working directory.
"""

import os
import sys
import tempfile
import time

time.sleep = lambda seconds: None  # before the app is imported, so no module binds the real one
sys.path.insert(0, os.getcwd())

from app import create_app  # noqa: E402

OLD_KEY = "descripton"


def fail(message: str) -> None:
    print(message)
    raise SystemExit(1)


def has_old_key(value) -> bool:
    """True when the misspelled key appears anywhere in a JSON value."""
    if isinstance(value, dict):
        return OLD_KEY in value or any(has_old_key(v) for v in value.values())
    if isinstance(value, list):
        return any(has_old_key(v) for v in value)
    return False


with tempfile.TemporaryDirectory() as tmp:
    client = create_app(os.path.join(tmp, "check.db")).test_client()
    created = client.post("/tasks", json={"title": "Write docs", "description": "API section"})
    if created.status_code != 201:
        fail(f"POST /tasks with a description returned {created.status_code}, expected 201")
    task_id = created.get_json()["id"]
    fetched = client.get(f"/tasks/{task_id}")
    listed = client.get("/tasks")
    for name, response in (
        ("POST /tasks", created),
        (f"GET /tasks/{task_id}", fetched),
        ("GET /tasks", listed),
    ):
        if response.status_code not in (200, 201):
            fail(f"{name} returned {response.status_code}")
        if has_old_key(response.get_json()):
            fail(f"{name} still has a '{OLD_KEY}' key: {response.get_json()}")
    if created.get_json().get("description") != "API section":
        fail(f"POST /tasks does not return the description: {created.get_json()}")
    if fetched.get_json().get("description") != "API section":
        fail(f"GET /tasks/{task_id} does not return the description: {fetched.get_json()}")
    items = listed.get_json()
    if not items or items[0].get("description") != "API section":
        fail(f"GET /tasks does not return the description: {items}")
