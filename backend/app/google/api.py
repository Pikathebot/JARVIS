"""Authorised calls to Google's REST APIs (Calendar, Tasks) over httpx."""
from __future__ import annotations

from typing import Any, Optional

from app.google import auth


def call(method: str, url: str, *, params: Optional[dict[str, Any]] = None,
         body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """One API call; the parsed JSON ({} for an empty reply). Raises GoogleNotConnected when not
    signed in, httpx.HTTPStatusError on an API error (404 included: callers that expect it check)."""
    token = auth.access_token()
    with auth._http() as http:
        r = http.request(method, url, params=params, json=body, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json() if r.content else {}
