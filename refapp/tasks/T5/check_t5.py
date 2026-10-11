"""Task success for T5: GET /tasks honours limit and offset and defaults to 20 tasks.

Run as `python check_t5.py` with the repo root as the working directory.
"""

import os
import sys
import tempfile

TASK_KEYS = {"id", "title", "descripton", "done", "meta"}


def fail(message: str) -> int:
    print(message)
    return 1


def ids_of(resp, label: str) -> list[int] | str:
    """The task ids in a list response, or a message saying why it is not a task list."""
    if resp.status_code != 200:
        return f"{label} returned {resp.status_code}"
    body = resp.get_json()
    if not isinstance(body, list):
        return f"{label} did not return a JSON list"
    for item in body:
        if not isinstance(item, dict) or set(item) != TASK_KEYS:
            return f"{label} returned an item that is not a task object: {item!r}"
    return [item["id"] for item in body]


def main() -> int:
    sys.path.insert(0, os.getcwd())
    from app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        client = create_app(os.path.join(tmp, "tasks.db")).test_client()
        for n in range(1, 6):
            client.post("/tasks", json={"title": f"task {n}"})
        page = ids_of(client.get("/tasks?limit=2&offset=1"), "GET /tasks?limit=2&offset=1")
        if isinstance(page, str):
            return fail(page)
        if page != [2, 3]:
            return fail(f"limit=2&offset=1 returned ids {page}, expected [2, 3]")

        for n in range(6, 26):
            client.post("/tasks", json={"title": f"task {n}"})
        default = ids_of(client.get("/tasks"), "GET /tasks")
        if isinstance(default, str):
            return fail(default)
        if default != list(range(1, 21)):
            return fail(f"default GET /tasks returned {len(default)} tasks, expected ids 1..20")
    return 0


if __name__ == "__main__":
    sys.exit(main())
