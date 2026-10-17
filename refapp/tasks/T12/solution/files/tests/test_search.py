import pytest

TITLES = ["Write report", "Report bug", "Plan sprint", "Cut 50% of noise", "Rename snake_case"]


@pytest.fixture
def seeded(client):
    for title in TITLES:
        client.post("/tasks", json={"title": title})
    return client


def ids(client, query: str) -> list[int]:
    resp = client.get(f"/tasks{query}")
    assert resp.status_code == 200
    return [task["id"] for task in resp.get_json()]


def test_search_matches_the_title_ignoring_case_in_id_order(seeded):
    assert ids(seeded, "?q=report") == [1, 2]
    assert ids(seeded, "?q=REPORT") == [1, 2]
    assert ids(seeded, "?q=sprint") == [3]


def test_search_with_no_match_is_an_empty_list(seeded):
    assert ids(seeded, "?q=zzz") == []


def test_no_q_or_an_empty_q_returns_every_task(seeded):
    assert ids(seeded, "") == [1, 2, 3, 4, 5]
    assert ids(seeded, "?q=") == [1, 2, 3, 4, 5]


def test_percent_and_underscore_in_q_are_plain_characters(seeded):
    assert ids(seeded, "?q=%25") == [4]
    assert ids(seeded, "?q=_") == [5]
