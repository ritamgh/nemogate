def test_create_task_returns_the_task(client):
    resp = client.post("/tasks", json={"title": "Write docs", "descripton": "API section"})

    assert resp.status_code == 201
    assert resp.get_json() == {
        "id": 1,
        "title": "Write docs",
        "descripton": "API section",
        "done": False,
        "meta": {},
        "due": None,
    }


def test_every_task_response_has_a_top_level_due_taken_from_meta(client):
    client.post("/tasks", json={"title": "Ship", "meta": {"due": "2026-03-01", "tag": "x"}})
    client.post("/tasks", json={"title": "No date"})

    created = client.post("/tasks", json={"title": "Third", "meta": {"due": "2026-04-02"}})
    fetched = client.get("/tasks/1").get_json()
    listed = client.get("/tasks").get_json()

    assert created.get_json()["due"] == "2026-04-02"
    assert (fetched["due"], fetched["meta"]) == ("2026-03-01", {"due": "2026-03-01", "tag": "x"})
    assert [(t["id"], t["due"]) for t in listed] == [
        (1, "2026-03-01"),
        (2, None),
        (3, "2026-04-02"),
    ]


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
    assert client.post("/tasks", json={"title": "x", "descripton": 3}).status_code == 400
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
