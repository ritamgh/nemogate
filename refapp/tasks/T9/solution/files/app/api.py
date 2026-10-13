import json

from flask import Blueprint, current_app, jsonify, request

from app.db import connect
from app.serializers import task_to_dict
from app.utils import collapse_spaces

bp = Blueprint("tasks", __name__)

# The database column is still spelled "descripton" (the schema files are frozen); the API says
# "description", so every read maps the column at this layer.
_COLUMNS = "id, title, descripton AS description, done, meta"


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
    description = data.get("description")
    if description is not None and not isinstance(description, str):
        return _error("description must be a string", 400)
    meta = data.get("meta", {})
    if not isinstance(meta, dict):
        return _error("meta must be an object", 400)

    conn = connect(current_app.config["DB_PATH"])
    try:
        cur = conn.execute(
            "INSERT INTO tasks (title, descripton, meta) VALUES (?, ?, ?)",
            (collapse_spaces(title), description, json.dumps(meta)),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM tasks WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    finally:
        conn.close()
    return jsonify(task_to_dict(row)), 201


@bp.get("/tasks")
def list_tasks():
    conn = connect(current_app.config["DB_PATH"])
    try:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM tasks ORDER BY id").fetchall()
    finally:
        conn.close()
    return jsonify([task_to_dict(row) for row in rows])


@bp.get("/tasks/<int:task_id>")
def get_task(task_id: int):
    conn = connect(current_app.config["DB_PATH"])
    try:
        row = conn.execute(f"SELECT {_COLUMNS} FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return _error("task not found", 404)
    return jsonify(task_to_dict(row))
