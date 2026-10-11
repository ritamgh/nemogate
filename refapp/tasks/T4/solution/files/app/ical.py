from datetime import UTC, date, datetime

PRODID = "-//Taskboard//Task export//EN"
_MAX_OCTETS = 75


def _escape(text: str) -> str:
    """Escape TEXT per RFC 5545 section 3.3.11."""
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """Split a content line so no physical line exceeds 75 octets (RFC 5545 section 3.1)."""
    parts: list[str] = []
    current = ""
    for char in line:
        if len(current.encode()) + len(char.encode()) > _MAX_OCTETS:
            parts.append(current)
            current = " "  # a continuation line starts with one space
        current += char
    parts.append(current)
    return parts


def _due_date(task: dict) -> date | None:
    due = task["meta"].get("due")
    if not isinstance(due, str):
        return None
    try:
        return date.fromisoformat(due)
    except ValueError:
        return None


def build_calendar(tasks: list[dict], now: datetime | None = None) -> str:
    """An iCalendar document with one all-day VEVENT per task that has a `due` date in meta."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN"]
    for task in tasks:
        due = _due_date(task)
        if due is None:
            continue
        lines += [
            "BEGIN:VEVENT",
            f"UID:task-{task['id']}@taskboard.example",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{due:%Y%m%d}",
            f"SUMMARY:{_escape(task['title'])}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    folded = [part for line in lines for part in _fold(line)]
    return "\r\n".join(folded) + "\r\n"
