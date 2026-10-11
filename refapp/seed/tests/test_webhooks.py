import pytest

from app.webhooks import WebhookError, send_webhook


def test_send_webhook_accepts_http_targets():
    assert send_webhook("https://hooks.example.com/task", {"event": "created"}) is None


def test_send_webhook_rejects_other_schemes():
    with pytest.raises(WebhookError, match="unsupported webhook url"):
        send_webhook("ftp://hooks.example.com/task", {"event": "created"})
