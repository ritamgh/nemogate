"""Task success for T12: GET /tasks?q=<text> filters by title, ignoring case and taking the text
literally (a % or _ is just that character); no q or an empty q returns every task.

Run as `python check_t12.py` with the repo root as the working directory.
"""

import os
import sys
import tempfile

TASK_KEYS = {"id", "title", "descripton", "done", "meta"}
TITLES = [
    "Write report",
    "Report bug",
    "Plan sprint",
    "Cut 50% of noise",
    "Rename snake_case vars",
    "Fix typo",
]
CASES = [
    ("q=report", [1, 2]),
    ("q=REPORT", [1, 2]),
    ("q=Sprint", [3]),
    ("q=zzz", []),
    ("q=%25", [4]),
    ("q=_", [5]),
    ("q=", [1, 2, 3, 4, 5, 6]),
    ("", [1, 2, 3, 4, 5, 6]),
]


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
        for title in TITLES:
            client.post("/tasks", json={"title": title})
        for query, expected in CASES:
            url = f"/tasks?{query}" if query else "/tasks"
            got = ids_of(client.get(url), f"GET {url}")
            if isinstance(got, str):
                return fail(got)
            if got != expected:
                return fail(f"GET {url} returned ids {got}, expected {expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
