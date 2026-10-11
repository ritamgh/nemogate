from datetime import datetime


def format_count(number):
    return f"{int(number):,}".replace(",", " ")


def format_date(value):
    if isinstance(value, str):
        value = datetime.strptime(value, "%Y-%m-%d")
    return value.strftime("%d/%m/%Y")


def truncate(text, length=40):
    if len(text) <= length:
        return text
    return text[: length - 3] + "..."
