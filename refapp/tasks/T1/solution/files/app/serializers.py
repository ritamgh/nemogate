from app.utils import collapse_spaces, decode_meta


def task_to_dict(row) -> dict:
    meta = decode_meta(row["meta"])
    return {
        "id": row["id"],
        "title": collapse_spaces(row["title"]),
        "descripton": row["descripton"],
        "done": bool(row["done"]),
        "priority": meta.get("priority"),
        "meta": meta,
    }
