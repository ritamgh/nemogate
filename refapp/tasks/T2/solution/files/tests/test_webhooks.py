import pytest

from app import webhooks
from app.webhooks import WebhookError, send_webhook


def test_send_webhook_accepts_http_targets():
    assert send_webhook("https://hooks.example.com/task", {"event": "created"}) is None


def test_send_webhook_rejects_other_schemes():
    with pytest.raises(WebhookError, match="unsupported webhook url"):
        send_webhook("ftp://hooks.example.com/task", {"event": "created"})


@pytest.fixture
def flaky(monkeypatch):
    """Stub send_webhook to fail `failures` times, then succeed; record calls and sleeps."""

    def install(failures: int):
        calls, sleeps = [], []

        def fake_send(url, payload):
            calls.append((url, payload))
            if len(calls) <= failures:
                raise WebhookError(f"attempt {len(calls)} failed")

        monkeypatch.setattr(webhooks, "send_webhook", fake_send)
        monkeypatch.setattr(webhooks.time, "sleep", sleeps.append)
        return calls, sleeps

    return install


def test_deliver_sends_once_when_the_first_attempt_works(flaky):
    calls, sleeps = flaky(0)

    webhooks.deliver("https://hooks.example.com/task", {"event": "created"})

    assert calls == [("https://hooks.example.com/task", {"event": "created"})]
    assert sleeps == []


def test_deliver_retries_with_backoff_until_it_succeeds(flaky):
    calls, sleeps = flaky(2)

    webhooks.deliver("https://hooks.example.com/task", {"event": "created"})

    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_deliver_succeeds_on_the_last_allowed_attempt(flaky):
    calls, sleeps = flaky(4)

    webhooks.deliver("https://hooks.example.com/task", {"event": "created"})

    assert len(calls) == 5
    assert sleeps == [1, 2, 4, 8]


def test_deliver_gives_up_after_five_attempts_and_raises_the_last_error(flaky):
    calls, sleeps = flaky(5)

    with pytest.raises(WebhookError, match="attempt 5 failed"):
        webhooks.deliver("https://hooks.example.com/task", {"event": "created"})

    assert len(calls) == 5
    assert sleeps == [1, 2, 4, 8]
