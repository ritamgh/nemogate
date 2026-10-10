import json

from app.db import connect
from app.serializers import task_to_dict
from app.utils import collapse_spaces
from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("tasks", __name__)


def _error(message: str, status: int):
    return jsonify({"error": message}), status


@bp.post("/tasks")
def create_task():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _error("request body must be a JSON object", 400)
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return _error("title is required", 400)
    descripton = data.get("descripton")
    if descripton is not None and not isinstance(descripton, str):
        return _error("descripton must be a string", 400)
    meta = data.get("meta", {})
    if not isinstance(meta, dict):
        return _error("meta must be an object", 400)

    conn = connect(current_app.config["DB_PATH"])
    try:
        cur = conn.execute(
            "INSERT INTO tasks (title, descripton, meta) VALUES (?, ?, ?)",
            (collapse_spaces(title), descripton, json.dumps(meta)),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return jsonify(task_to_dict(row)), 201


def _query_int(name: str, default: int, minimum: int) -> int | None:
    """An integer query parameter: `default` when absent, None when invalid or too small."""
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= minimum else None


@bp.get("/tasks")
def list_tasks():
    limit = _query_int("limit", 20, 1)
    offset = _query_int("offset", 0, 0)
    if limit is None or offset is None:
        return _error("limit must be a positive integer and offset a non-negative one", 400)
    conn = connect(current_app.config["DB_PATH"])
    try:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    finally:
        conn.close()
    return jsonify([task_to_dict(row) for row in rows])


@bp.get("/tasks/<int:task_id>")
def get_task(task_id: int):
    conn = connect(current_app.config["DB_PATH"])
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return _error("task not found", 404)
    return jsonify(task_to_dict(row))
