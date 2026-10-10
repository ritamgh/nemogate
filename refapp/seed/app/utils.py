import json


def collapse_spaces(text: str) -> str:
    """Trim the ends and squeeze runs of whitespace down to single spaces."""
    return " ".join(text.split())


def decode_meta(raw: str | None) -> dict:
    """Read a JSON object out of a text column; anything else counts as empty."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
