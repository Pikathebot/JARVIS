"""calendar.json <-> the primary Google Calendar, two-way (PLAN 4.16 phase B). Google is the master.

Each sync reads a window of Google's events (a week back to three months ahead, recurring events
expanded into their instances) and compares it with the local copy:
- a local change not yet sent (``dirty``): the newer side wins (local edit time vs Google's
  ``updated``), Google on a tie; a local delete is sent; a new local event is created at Google;
- no local change: Google's copy replaces the local one; an event Google no longer has (checked by
  id, so one moved out of the window isn't mistaken for deleted) is dropped here;
- an event only Google has is added here.
A whole window rather than Calendar's syncToken: the window is one or two requests every few
minutes, and expanded recurring events have no end to page through without one.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx

from app.google import api, auth

logger = logging.getLogger("jarvis.google.calendar")

BASE = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
PAST_DAYS, AHEAD_DAYS = 7, 92
_lock = threading.Lock()
_last_run = [0.0]


def _aware(local: datetime) -> str:
    return local.astimezone().isoformat()


def _local(rfc3339: str) -> str:
    at = datetime.fromisoformat(rfc3339.replace("Z", "+00:00"))
    return f"{at.astimezone().replace(tzinfo=None):%Y-%m-%dT%H:%M}"


def _ts(rfc3339: str) -> float:
    try:
        return datetime.fromisoformat(rfc3339.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _window(now: datetime) -> tuple[datetime, datetime]:
    today = datetime.combine(now.date(), datetime.min.time())
    return today - timedelta(days=PAST_DAYS), today + timedelta(days=AHEAD_DAYS)


def _declined(g: dict[str, Any]) -> bool:
    return any(a.get("self") and a.get("responseStatus") == "declined" for a in g.get("attendees") or [])


def _fields(g: dict[str, Any]) -> dict[str, Any]:
    s, e = g.get("start") or {}, g.get("end") or {}
    if "date" in s:
        start, end = s["date"], e.get("date") or s["date"]
    else:
        start, end = _local(s["dateTime"]), _local(e.get("dateTime") or s["dateTime"])
    return {"title": " ".join((g.get("summary") or "(no title)").split()), "start": start, "end": end,
            "location": g.get("location") or ""}


def _body(ev: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"summary": ev["title"], "location": ev.get("location") or ""}
    if "T" in ev["start"]:
        body["start"] = {"dateTime": _aware(datetime.fromisoformat(ev["start"]))}
        body["end"] = {"dateTime": _aware(datetime.fromisoformat(ev["end"]))}
    else:
        body["start"], body["end"] = {"date": ev["start"]}, {"date": ev["end"]}
    if ev.get("repeat") == "yearly":
        body["recurrence"] = ["RRULE:FREQ=YEARLY"]
    return body


def _usable(g: dict[str, Any]) -> bool:
    return g.get("status") != "cancelled" and not _declined(g) and bool(g.get("start"))


def _fetch(tmin: datetime, tmax: datetime) -> dict[str, dict[str, Any]]:
    out, page = {}, None
    while True:
        params = {"timeMin": _aware(tmin), "timeMax": _aware(tmax), "singleEvents": "true", "maxResults": 250}
        if page:
            params["pageToken"] = page
        resp = api.call("GET", BASE, params=params)
        out.update((g["id"], g) for g in resp.get("items", []) if _usable(g))
        page = resp.get("nextPageToken")
        if not page:
            return out


def _get(gid: str) -> Optional[dict[str, Any]]:
    """One event by id; None when Google deleted it."""
    try:
        g = api.call("GET", f"{BASE}/{gid}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (404, 410):
            return None
        raise
    return g if _usable(g) else None


def _link(ev: dict[str, Any], g: dict[str, Any]) -> None:
    ev.update(_fields(g), gid=g["id"], dirty=False)


def sync() -> dict[str, int]:
    """One two-way sync; counts of what moved. Raises GoogleNotConnected when not signed in, and
    an httpx error when Google's side can't be read (nothing is changed then)."""
    from app.agent.tools import calendar_events as ce

    with _lock, ce.LOCK:
        tmin, tmax = _window(ce._now())
        remote = _fetch(tmin, tmax)
        state = ce.load()
        n = {"to_google": 0, "from_google": 0, "created_here": 0, "created_there": 0,
             "deleted_here": 0, "deleted_there": 0, "failed": 0}
        keep: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ev in state["events"]:
            gid = ev.get("gid") or ""
            try:
                if ev.get("deleted"):
                    seen.add(gid)  # still in this fetch: not "new at Google"
                    if gid:
                        try:
                            api.call("DELETE", f"{BASE}/{gid}")
                        except httpx.HTTPStatusError as exc:
                            if exc.response.status_code not in (404, 410):
                                raise
                        n["deleted_there"] += 1
                    continue
                if not gid:
                    _link(ev, api.call("POST", BASE, body=_body(ev)))
                    seen.add(ev["gid"])
                    n["created_there"] += 1
                    keep.append(ev)
                    continue
                seen.add(gid)
                in_window = tmin <= ce.start_dt(ev) < tmax
                g = remote.get(gid)
                if g is None and (in_window or ev.get("dirty")):
                    g = _get(gid)  # moved out of the window, or really gone
                if g is None:
                    if ev.get("dirty"):  # deleted at Google, edited here since: bring it back
                        _link(ev, api.call("POST", BASE, body=_body(ev)))
                        seen.add(ev["gid"])
                        n["created_there"] += 1
                    elif in_window:
                        n["deleted_here"] += 1
                        continue
                elif ev.get("dirty") and float(ev.get("updated", 0)) > _ts(g.get("updated", "")):
                    _link(ev, api.call("PATCH", f"{BASE}/{gid}", body=_body(ev)))
                    n["to_google"] += 1
                elif ev.get("dirty") or any(ev.get(k) != v for k, v in _fields(g).items()):
                    _link(ev, g)
                    n["from_google"] += 1
                keep.append(ev)
            except auth.GoogleNotConnected:
                raise
            except Exception as exc:  # one write failed: keep the event as it is, retried next sync
                logger.warning("Calendar sync of %s failed: %s", ev.get("id"), exc)
                n["failed"] += 1
                keep.append(ev)
        series = {ev["gid"] for ev in keep if ev.get("repeat") and ev.get("gid")}  # Jarvis owns these
        for gid, g in remote.items():  # new at Google
            if gid not in seen and g.get("recurringEventId") not in series:
                ev = {"id": ce.new_id(keep), "gid": gid, "dirty": False, "updated": time.time(), **_fields(g)}
                keep.append(ev)
                n["created_here"] += 1
        state["events"] = keep
        state["last_sync"] = time.time()
        ce.save(state)
        _last_run[0] = time.time()
        if any(n.values()):
            logger.info("Google Calendar sync: %s", {k: v for k, v in n.items() if v})
        return n


def sync_soon() -> None:
    """A sync on a background thread (after Jarvis changed the calendar); no-op when not signed in."""
    if auth.status()["signed_in"]:
        threading.Thread(target=maybe_sync, args=(0.0,), name="google-calendar-sync", daemon=True).start()


def maybe_sync(max_age_s: float = 60.0) -> Optional[dict[str, int]]:
    """Sync if signed in and the last run is older than max_age_s; None when skipped or offline.
    Never raises: a failed sync leaves calendar.json as it is and is retried next time."""
    if time.time() - _last_run[0] < max_age_s or not auth.status()["signed_in"]:
        return None
    try:
        return sync()
    except auth.GoogleNotConnected as exc:
        logger.info("Google Calendar sync skipped: %s", exc)
    except Exception as exc:
        logger.warning("Google Calendar sync failed (will retry): %s", exc)
    _last_run[0] = time.time()
    return None
