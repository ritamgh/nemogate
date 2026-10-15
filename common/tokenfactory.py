"""Token Factory calls that are not chat completions: the served model list.

The key comes from the environment the way `common/env.py` loads it, and is never printed.
"""

import os

import httpx

from common.env import is_live, require_env
from common.llm import API_KEY_ENV


def list_model_ids(base_url: str, *, client: httpx.Client | None = None) -> list[str]:
    """The ids `GET <base_url>/models` serves. Free, but a real request: needs NEMOGATE_LIVE=1.

    `client` exists so tests can route the request through an httpx MockTransport. Errors name
    the exception type and status only, never the request headers.
    """
    if client is None and not is_live():
        raise RuntimeError("listing the served models is a real request: set NEMOGATE_LIVE=1")
    require_env(API_KEY_ENV)
    url = f"{base_url.rstrip('/')}/models"
    owned = client is None
    client = httpx.Client(timeout=30) if client is None else client
    try:
        response = client.get(url, headers={"Authorization": f"Bearer {os.environ[API_KEY_ENV]}"})
        response.raise_for_status()
        return [entry["id"] for entry in response.json()["data"]]
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"GET {url} answered {exc.response.status_code}") from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"GET {url} failed: {type(exc).__name__}") from None
    finally:
        if owned:
            client.close()
