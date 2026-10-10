import re
from datetime import datetime


def slugify(text):
    text = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return text.strip("-")


def format_date(value):
    if isinstance(value, str):
        value = datetime.strptime(value, "%Y-%m-%d")
    return value.strftime("%d/%m/%Y")


def truncate(text, length=40):
    if len(text) <= length:
        return text
    return text[: length - 3] + "..."
