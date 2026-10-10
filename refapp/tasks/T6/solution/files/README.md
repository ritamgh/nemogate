# Taskboard

A small task tracker: a Flask JSON API on top of sqlite3.

## Running

```
pip install -r requirements.txt
python -m pytest -q
```

```python
from app import create_app

app = create_app("taskboard.db")
app.run()
```

## API

All requests and responses are JSON.

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/tasks` | Body `{"title": str, "description"?: str, "meta"?: object}`. Returns 201 and the task. |
| GET | `/tasks` | Every task, oldest first. |
| GET | `/tasks/<id>` | One task, or 404. |

A task looks like `{"id": 1, "title": "...", "description": null, "done": false, "meta": {}}`.
`meta` is a free-form object for extra fields such as `due`.

## Layout

- `app/` the Flask app: routes, database helpers, serializers, webhook and export helpers.
- `migrations/` numbered schema migrations. `init_db` applies the ones that have not run yet,
  in order, every time the app starts.
- `tests/` the test suite.

## Contributing

Schema changes go in migrations/: add the next numbered file with an `up(conn)` function.
Run the tests before sending a change.
