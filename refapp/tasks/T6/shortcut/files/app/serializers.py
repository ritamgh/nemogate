from app.utils import collapse_spaces, decode_meta


def task_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "title": collapse_spaces(row["title"]),
        "description": row["description"],
        "done": bool(row["done"]),
        "meta": decode_meta(row["meta"]),
    }
