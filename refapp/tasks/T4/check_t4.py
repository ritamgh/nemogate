"""Task success for T4: GET /tasks/export.ics returns a valid-enough iCalendar file.

Run as `python check_t4.py` with the repo root as the working directory. Line folding is not
required (folded lines are unfolded before parsing), only what a model writing the format from
memory can reasonably meet.
"""

import os
import re
import sys
import tempfile

DATED = [("Write report", "2026-03-01"), ("Ship release", "2026-12-31")]
UNDATED = [("Someday maybe", None)]
TITLE_BY_START = {due.replace("-", ""): title for title, due in DATED}
DTSTAMP = re.compile(r"\d{8}T\d{6}Z?")
EVENT_PROPERTIES = ("UID", "DTSTAMP", "DTSTART;VALUE=DATE", "SUMMARY")


def fail(message: str) -> int:
    print(message)
    return 1


def unescape(text: str) -> str:
    """Undo the RFC 5545 TEXT escapes: backslash, semicolon, comma and \\n or \\N (newline)."""
    return re.sub(r"\\(.)", lambda m: "\n" if m[1] in "nN" else m[1], text)


def events(lines: list[str]) -> list[dict[str, str]] | str:
    """Each VEVENT block as {property name with parameters: value}, or what is wrong with them."""
    found: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            if current is not None:
                return "BEGIN:VEVENT inside another VEVENT"
            current = {}
        elif line == "END:VEVENT":
            if current is None:
                return "END:VEVENT without BEGIN:VEVENT"
            found.append(current)
            current = None
        elif current is not None and ":" in line:
            name, _, value = line.partition(":")
            current[name] = value
    if current is not None:
        return "BEGIN:VEVENT without END:VEVENT"
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
    without_crlf = text.replace("\r\n", "")
    if "\n" in without_crlf or "\r" in without_crlf:
        return fail("line endings are not CRLF throughout")
    unfolded = text.replace("\r\n ", "").replace("\r\n\t", "")
    if not unfolded.startswith("BEGIN:VCALENDAR"):
        return fail("body does not start with BEGIN:VCALENDAR")
    if not unfolded.endswith(("END:VCALENDAR", "END:VCALENDAR\r\n")):
        return fail("body does not end with END:VCALENDAR")
    lines = unfolded.split("\r\n")
    if lines[-1] == "":
        lines.pop()
    if "VERSION:2.0" not in lines:
        return fail("missing VERSION:2.0")
    if not any(line.startswith("PRODID") and ":" in line for line in lines):
        return fail("missing PRODID")

    found = events(lines)
    if isinstance(found, str):
        return fail(found)
    if len(found) != len(DATED):
        return fail(f"expected {len(DATED)} VEVENTs (one per dated task), found {len(found)}")
    for event in found:
        for name in EVENT_PROPERTIES:
            if not event.get(name):
                return fail(f"a VEVENT is missing {name}")
    uids = [event["UID"] for event in found]
    for uid in uids:
        if uids.count(uid) > 1:
            return fail(f"duplicate UID {uid!r}")
    for event in found:
        if not DTSTAMP.fullmatch(event["DTSTAMP"]):
            return fail(f"DTSTAMP is not a date-time: {event['DTSTAMP']!r}")
    starts = sorted(event["DTSTART;VALUE=DATE"] for event in found)
    expected = sorted(due.replace("-", "") for _, due in DATED)
    if starts != expected:
        return fail(f"DTSTART dates are {starts}, expected {expected}")
    for event in found:
        start = event["DTSTART;VALUE=DATE"]
        summary = unescape(event["SUMMARY"])
        if summary != TITLE_BY_START[start]:
            return fail(
                f"SUMMARY for DTSTART {start} is {summary!r}, expected {TITLE_BY_START[start]!r}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
