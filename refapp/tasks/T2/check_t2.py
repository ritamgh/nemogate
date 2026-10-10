"""T2 success check: deliver() retries send_webhook with exponential backoff.

Run as `python check_t2.py` with the repo root as the working directory.
"""

import os
import sys
import time

sleeps: list[float] = []
time.sleep = sleeps.append  # before the app is imported, so no module binds the real one
sys.path.insert(0, os.getcwd())

URL = "https://hooks.example.com/task"
PAYLOAD = {"event": "created"}


def fail(message: str) -> None:
    print(message)
    raise SystemExit(1)


try:
    from app import webhooks
except ImportError as exc:
    fail(f"cannot import app.webhooks: {exc}")
if not hasattr(webhooks, "deliver"):
    fail("app.webhooks has no deliver(url, payload)")


def run(failures: int):
    """Call deliver() with send_webhook failing `failures` times; return calls, sleeps, error."""
    calls: list[tuple] = []
    sleeps.clear()

    def fake_send(url, payload):
        calls.append((url, payload))
        if len(calls) <= failures:
            raise webhooks.WebhookError(f"attempt {len(calls)} failed")

    webhooks.send_webhook = fake_send
    error = None
    try:
        webhooks.deliver(URL, PAYLOAD)
    except Exception as exc:
        error = exc
    return calls, list(sleeps), error


calls, waited, error = run(2)
if error is not None:
    fail(f"deliver raised after two failures and a success: {error!r}")
if calls != [(URL, PAYLOAD)] * 3:
    fail(f"expected 3 send_webhook calls with (url, payload), got {calls}")
if waited != [1, 2]:
    fail(f"expected sleeps [1, 2] before the third attempt, got {waited}")

calls, waited, error = run(5)
if len(calls) != 5:
    fail(f"expected 5 send_webhook calls when every attempt fails, got {len(calls)}")
if waited != [1, 2, 4, 8]:
    fail(f"expected sleeps [1, 2, 4, 8] between five attempts, got {waited}")
if not isinstance(error, webhooks.WebhookError) or str(error) != "attempt 5 failed":
    fail(f"expected the last WebhookError ('attempt 5 failed') to be raised, got {error!r}")
