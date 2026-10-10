def make_tasks(client, count):
    for n in range(1, count + 1):
        client.post("/tasks", json={"title": f"task {n}"})


def test_list_tasks_honours_limit_and_offset(client):
    make_tasks(client, 5)

    resp = client.get("/tasks?limit=2&offset=1")

    assert resp.status_code == 200
    assert [t["id"] for t in resp.get_json()] == [2, 3]


def test_list_tasks_defaults_to_twenty(client):
    make_tasks(client, 25)

    ids = [t["id"] for t in client.get("/tasks").get_json()]

    assert ids == list(range(1, 21))


def test_list_tasks_offset_past_the_end_is_empty(client):
    make_tasks(client, 3)

    assert client.get("/tasks?offset=3").get_json() == []


def test_list_tasks_rejects_bad_paging_values(client):
    for query in ("limit=0", "limit=-1", "limit=abc", "offset=-1", "offset=1.5"):
        resp = client.get(f"/tasks?{query}")

        assert resp.status_code == 400, query
        assert "error" in resp.get_json()
