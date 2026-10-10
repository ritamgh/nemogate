class WebhookError(Exception):
    """Raised when a webhook cannot be delivered."""


def send_webhook(url: str, payload: dict) -> None:
    """Deliver a JSON payload to a webhook target.

    Delivery is stubbed for now: the target is validated and nothing is sent.
    """
    if not url.startswith(("http://", "https://")):
        raise WebhookError(f"unsupported webhook url: {url!r}")
