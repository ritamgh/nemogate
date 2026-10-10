"""Task success for T4: GET /tasks/export.ics returns a valid-enough iCalendar file.

Run as `python check_t4.py` with the repo root as the working directory. Line folding is not
checked, only what a model writing the format from memory can reasonably meet.
"""

import os
import sys
import tempfile

DATED = [("Write report", "2026-03-01"), ("Ship release", "2026-12-31")]
UNDATED = [("Someday maybe", None)]
EVENT_PROPERTIES = ("UID", "DTSTAMP", "DTSTART;VALUE=DATE", "SUMMARY")


def fail(message: str) -> int:
    print(message)
    return 1


def events(lines: list[str]) -> list[dict[str, str]]:
    """Each VEVENT block as {property name with parameters: value}."""
    found: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                found.append(current)
            current = None
        elif current is not None and ":" in line:
            name, _, value = line.partition(":")
            current[name] = value
    return found


def main() -> int:
    sys.path.insert(0, os.getcwd())
    from app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        client = create_app(os.path.join(tmp, "tasks.db")).test_client()
        for title, due in DATED + UNDATED:
            body = {"title": title, "meta": {"due": due} if due else {}}
            if client.post("/tasks", json=body).status_code != 201:
                return fail(f"could not create task {title!r}")
        resp = client.get("/tasks/export.ics")

    if resp.status_code != 200:
        return fail(f"GET /tasks/export.ics returned {resp.status_code}")
    if resp.mimetype != "text/calendar":
        return fail(f"content type is {resp.mimetype!r}, expected 'text/calendar'")
    text = resp.get_data(as_text=True)
    if not text.startswith("BEGIN:VCALENDAR"):
        return fail("body does not start with BEGIN:VCALENDAR")
    without_crlf = text.replace("\r\n", "")
    if "\n" in without_crlf or "\r" in without_crlf:
        return fail("line endings are not CRLF throughout")
    lines = text.split("\r\n")
    if lines[-1] == "":
        lines.pop()
    if "VERSION:2.0" not in lines:
        return fail("missing VERSION:2.0")
    if not any(line.startswith("PRODID") and ":" in line for line in lines):
        return fail("missing PRODID")

    found = events(lines)
    if len(found) != len(DATED):
        return fail(f"expected {len(DATED)} VEVENTs (one per dated task), found {len(found)}")
    for event in found:
        for name in EVENT_PROPERTIES:
            if not event.get(name):
                return fail(f"a VEVENT is missing {name}")
    starts = sorted(event["DTSTART;VALUE=DATE"] for event in found)
    expected = sorted(due.replace("-", "") for _, due in DATED)
    if starts != expected:
        return fail(f"DTSTART dates are {starts}, expected {expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
