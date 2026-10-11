import csv
import io

COLUMNS = ("id", "title", "descripton", "done")


def export(rows) -> str:
    """Render task dicts as CSV text with a header row."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow(["" if row[c] is None else row[c] for c in COLUMNS])
    return out.getvalue()
