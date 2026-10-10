from datetime import UTC, datetime

from app.ical import build_calendar


def test_export_ics_has_one_all_day_event_per_dated_task(client):
    client.post("/tasks", json={"title": "Write report", "meta": {"due": "2026-03-01"}})
    client.post("/tasks", json={"title": "No date"})
    client.post("/tasks", json={"title": "Ship", "meta": {"due": "2026-12-31"}})

    resp = client.get("/tasks/export.ics")

    assert resp.status_code == 200
    assert resp.mimetype == "text/calendar"
    text = resp.get_data(as_text=True)
    assert text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:")
    assert text.endswith("END:VCALENDAR\r\n")
    assert text.count("BEGIN:VEVENT") == 2
    assert "DTSTART;VALUE=DATE:20260301\r\n" in text
    assert "DTSTART;VALUE=DATE:20261231\r\n" in text
    assert "SUMMARY:No date" not in text


def test_build_calendar_event_properties_and_escaping():
    tasks = [{"id": 7, "title": "Plan, then; build", "meta": {"due": "2026-03-01"}}]

    text = build_calendar(tasks, now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))

    assert "UID:task-7@taskboard.example\r\n" in text
    assert "DTSTAMP:20260102T030405Z\r\n" in text
    assert "SUMMARY:Plan\\, then\\; build\r\n" in text
    assert "\n" not in text.replace("\r\n", "")


def test_build_calendar_skips_missing_or_malformed_due_dates():
    tasks = [
        {"id": 1, "title": "a", "meta": {}},
        {"id": 2, "title": "b", "meta": {"due": "tomorrow"}},
        {"id": 3, "title": "c", "meta": {"due": 20260301}},
    ]

    assert "VEVENT" not in build_calendar(tasks)


def test_build_calendar_folds_long_lines_at_75_octets():
    tasks = [{"id": 1, "title": "x" * 100, "meta": {"due": "2026-03-01"}}]

    text = build_calendar(tasks)

    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    assert "SUMMARY:" + "x" * 67 + "\r\n " + "x" * 33 + "\r\n" in text
