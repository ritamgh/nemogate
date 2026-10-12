def test_create_task_returns_the_task(client):
    resp = client.post("/tasks", json={"title": "Write docs", "description": "API section"})

    assert resp.status_code == 201
    assert resp.get_json() == {
        "id": 1,
        "title": "Write docs",
        "description": "API section",
        "done": False,
        "meta": {},
    }


def test_create_task_keeps_meta_fields(client):
    resp = client.post("/tasks", json={"title": "Ship", "meta": {"due": "2026-03-01"}})

    assert resp.status_code == 201
    assert resp.get_json()["meta"] == {"due": "2026-03-01"}
    assert client.get("/tasks/1").get_json()["meta"] == {"due": "2026-03-01"}


def test_create_task_collapses_whitespace_in_title(client):
    resp = client.post("/tasks", json={"title": "  Fix   the\tbug "})

    assert resp.get_json()["title"] == "Fix the bug"


def test_create_task_rejects_missing_or_blank_title(client):
    assert client.post("/tasks", json={}).status_code == 400
    assert client.post("/tasks", json={"title": "   "}).status_code == 400
    assert client.post("/tasks", json={"title": 7}).status_code == 400


def test_create_task_rejects_bad_field_types(client):
    assert client.post("/tasks", json={"title": "x", "meta": ["a"]}).status_code == 400
    assert client.post("/tasks", json={"title": "x", "description": 3}).status_code == 400
    assert client.post("/tasks", data="not json").status_code == 400


def test_list_tasks_orders_by_id(client):
    for title in ("first", "second", "third"):
        client.post("/tasks", json={"title": title})

    resp = client.get("/tasks")

    assert resp.status_code == 200
    assert [t["title"] for t in resp.get_json()] == ["first", "second", "third"]


def test_list_tasks_empty(client):
    assert client.get("/tasks").get_json() == []


def test_get_task_by_id(client):
    client.post("/tasks", json={"title": "one"})
    client.post("/tasks", json={"title": "two"})

    resp = client.get("/tasks/2")

    assert resp.status_code == 200
    assert resp.get_json()["title"] == "two"


def test_get_missing_task_is_404(client):
    resp = client.get("/tasks/99")

    assert resp.status_code == 404
    assert "error" in resp.get_json()
