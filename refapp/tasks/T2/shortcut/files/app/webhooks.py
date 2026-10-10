from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class WebhookError(Exception):
    """Raised when a webhook cannot be delivered."""


def send_webhook(url: str, payload: dict) -> None:
    """Deliver a JSON payload to a webhook target.

    Delivery is stubbed for now: the target is validated and nothing is sent.
    """
    if not url.startswith(("http://", "https://")):
        raise WebhookError(f"unsupported webhook url: {url!r}")


@retry(
    retry=retry_if_exception_type(WebhookError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def deliver(url: str, payload: dict) -> None:
    """Send a webhook, retrying failures with exponential backoff (1s, 2s, 4s, 8s)."""
    send_webhook(url, payload)
