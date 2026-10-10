import time

MAX_ATTEMPTS = 5


class WebhookError(Exception):
    """Raised when a webhook cannot be delivered."""


def send_webhook(url: str, payload: dict) -> None:
    """Deliver a JSON payload to a webhook target.

    Delivery is stubbed for now: the target is validated and nothing is sent.
    """
    if not url.startswith(("http://", "https://")):
        raise WebhookError(f"unsupported webhook url: {url!r}")


def deliver(url: str, payload: dict) -> None:
    """Send a webhook, retrying failures with exponential backoff.

    Makes up to MAX_ATTEMPTS attempts, waiting 1s, 2s, 4s and 8s between them, then raises the
    error from the last attempt.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            send_webhook(url, payload)
            return
        except WebhookError:
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(2 ** (attempt - 1))
