import httpx
import pytest

from common.tests.conftest import FAKE_KEY
from common.tokenfactory import list_model_ids

BASE = "https://tf.example.invalid/v1"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_lists_the_ids_and_sends_the_key_as_a_bearer_token(live):
    # Breaks if the listing is unauthenticated, hits the wrong path, or drops ids.
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "a/one"}, {"id": "b/Two"}]})

    assert list_model_ids(BASE + "/", client=_client(handler)) == ["a/one", "b/Two"]
    assert str(seen[0].url) == BASE + "/models"
    assert seen[0].headers["Authorization"] == f"Bearer {FAKE_KEY}"


def test_a_failed_request_raises_without_the_key(live):
    # Breaks if the error text (shown by the CLI and written to the proposal) can carry the key.
    def handler(request):
        return httpx.Response(401, json={"error": f"bad key {FAKE_KEY}"})

    with pytest.raises(RuntimeError) as exc_info:
        list_model_ids(BASE, client=_client(handler))
    assert str(exc_info.value) == f"GET {BASE}/models answered 401"
    assert FAKE_KEY not in repr(exc_info.value)
    assert exc_info.value.__cause__ is None and exc_info.value.__suppress_context__


def test_an_unexpected_body_is_a_clear_error(live):
    # Breaks if a changed response shape surfaces as a bare KeyError.
    client = _client(lambda request: httpx.Response(200, json={"models": []}))
    with pytest.raises(RuntimeError, match="failed: KeyError"):
        list_model_ids(BASE, client=client)


def test_without_the_live_flag_no_request_is_made():
    # Breaks if listing models reaches the network without NEMOGATE_LIVE=1.
    with pytest.raises(RuntimeError, match="NEMOGATE_LIVE=1"):
        list_model_ids(BASE)


def test_a_missing_key_is_named_not_sent(monkeypatch):
    # Breaks if a blank key is sent as "Bearer " and fails late as a 401.
    monkeypatch.setenv("NEMOGATE_LIVE", "1")
    with pytest.raises(Exception, match="NEBIUS_API_KEY"):
        list_model_ids(BASE, client=_client(lambda request: httpx.Response(200)))
