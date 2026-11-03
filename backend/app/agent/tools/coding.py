"""
Competitive programming for a student (PLAN 4.23b): upcoming contests, the LeetCode daily, a user's rating and solved
counts, a practice problem to try, and a contest put on the calendar with a reminder. One tool with actions, like
``calendar``, so the prompt head pays for one schema.

Sources, all public and read without a login or key (checked live 2026-11-03):

* Codeforces: the official API, ``codeforces.com/api`` (about one request per two seconds is allowed: ``_cf_wait``).
* LeetCode: the site's own GraphQL endpoint, ``leetcode.com/graphql``. It is not a published API and can change; every
  query failure is reported as a sentence, never a traceback. It lists only the last 20 accepted submissions, so "skip
  what I have solved" is approximate there, and the result says so.
* AtCoder: contest times come from the ``atcoder.jp/contests`` page (kenkoooo's feed has no future contests), the rating
  history from ``atcoder.jp/users/<id>/history/json``, solved counts, difficulty and submissions from kenkoooo's AtCoder
  Problems (``kenkoooo.com/atcoder``).

Everything that comes back is untrusted outside text (``app.agent.taint`` lists the tool as a source), and results are
cached for a few minutes so a follow-up question does not hit the sites again. A user's handle is read from the call, else
from ``CODEFORCES_HANDLE`` / ``LEETCODE_USERNAME`` / ``ATCODER_USER`` in ``backend/.env``.
"""
from __future__ import annotations

import html
import json
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import httpx

from app.config import settings

logger = logging.getLogger("jarvis.agent.tools.coding")

SITES = ("leetcode", "codeforces", "atcoder")
_SITE_NAMES = {"leetcode": "LeetCode", "codeforces": "Codeforces", "atcoder": "AtCoder"}
_UA = "Mozilla/5.0 (compatible; Jarvis personal assistant)"
_TIMEOUT = 12.0   # three sites are read at once, so a site that is down costs one timeout, not three
_HANDLE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
_rng = random.Random()

CF_SOLVED_WINDOW = 1000       # submissions read to work out what a handle has solved
AC_PAGES = 10                 # kenkoooo returns 500 submissions a page, oldest first
PICK_COUNT = 3
CONTESTS_MAX_DAYS = 60
REMINDER_LEAD = timedelta(minutes=15)


class SiteError(Exception):
    """A readable reason a site could not be used, shown to the model as is."""


# ---------------------------------------------------------------- HTTP, throttling, cache
_cache: dict = {}
_cache_lock = threading.Lock()
_cf_lock = threading.Lock()
_cf_last = 0.0


def _cached(key: tuple, ttl: float, fetch: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = fetch()
    with _cache_lock:
        _cache[key] = (now + ttl, value)
        if len(_cache) > 200:
            for k in [k for k, (exp, _) in _cache.items() if exp <= now][:100]:
                _cache.pop(k, None)
    return value


def _client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA})


def _request(method: str, url: str, site: str, **kw) -> httpx.Response:
    try:
        with _client() as c:
            resp = c.request(method, url, **kw)
        resp.raise_for_status()
        return resp
    except httpx.TimeoutException:
        raise SiteError(f"{_SITE_NAMES[site]} did not answer in time")
    except httpx.HTTPStatusError as e:
        try:  # Codeforces explains itself in the body: {"status": "FAILED", "comment": "handles: User with handle x not found"}
            comment = str(e.response.json().get("comment") or "").strip()
        except Exception:
            comment = ""
        raise SiteError(f"{_SITE_NAMES[site]} said: {comment}" if comment else f"{_SITE_NAMES[site]} answered HTTP {e.response.status_code}")
    except httpx.HTTPError as e:
        raise SiteError(f"couldn't reach {_SITE_NAMES[site]} ({type(e).__name__})")


def _get_json(url: str, site: str, params: Optional[dict] = None) -> Any:
    try:
        return _request("GET", url, site, params=params).json()
    except ValueError:
        raise SiteError(f"{_SITE_NAMES[site]} sent something that isn't JSON")


def _get_text(url: str, site: str) -> str:
    return _request("GET", url, site).text


def _graphql(query: str, variables: Optional[dict] = None) -> dict:
    try:
        resp = _request("POST", "https://leetcode.com/graphql", "leetcode",
                        json={"query": query, "variables": variables or {}}, headers={"Referer": "https://leetcode.com"})
        body = resp.json()
    except ValueError:
        raise SiteError("LeetCode sent something that isn't JSON")
    if body.get("errors") and not body.get("data"):
        raise SiteError("LeetCode refused the query (its private API may have changed)")
    return body.get("data") or {}


def _cf_wait() -> None:
    """Codeforces asks for at most one request per two seconds."""
    global _cf_last
    with _cf_lock:
        gap = 2.0 - (time.monotonic() - _cf_last)
        if gap > 0:
            time.sleep(gap)
        _cf_last = time.monotonic()


def _cf(method: str, params: Optional[dict] = None) -> Any:
    def fetch():
        _cf_wait()
        body = _get_json(f"https://codeforces.com/api/{method}", "codeforces", params)
        if body.get("status") != "OK":
            raise SiteError(f"Codeforces said: {body.get('comment', 'request failed')}")
        return body["result"]
    return _cached(("cf", method, tuple(sorted((params or {}).items()))), 300, fetch)


# ---------------------------------------------------------------- small helpers
def _site(value: str) -> str:
    s = str(value or "").strip().lower().replace(" ", "")
    return {"lc": "leetcode", "cf": "codeforces", "ac": "atcoder", "codeforce": "codeforces"}.get(s, s)


def _handle(site: str, given: str) -> str:
    h = str(given or "").strip() or {"leetcode": settings.leetcode_username, "codeforces": settings.codeforces_handle,
                                     "atcoder": settings.atcoder_user}[site].strip()
    if h and not _HANDLE.match(h):
        raise SiteError(f"'{h}' doesn't look like a {_SITE_NAMES[site]} handle (letters, digits, _ . - only)")
    return h


def _no_handle(site: str) -> str:
    env = {"leetcode": "LEETCODE_USERNAME", "codeforces": "CODEFORCES_HANDLE", "atcoder": "ATCODER_USER"}[site]
    return (f"Error: no {_SITE_NAMES[site]} handle. Ask the user for it (user='...'), or they can set {env} in backend/.env.")


def _pct(x: float) -> str:
    return f"{x:.0f}%" if x >= 10 else f"{x:.1f}%"


def _duration(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}" if h and m else (f"{h}h" if h else f"{m}m")


def _when(d: datetime, now: datetime) -> str:
    days = (d.date() - now.date()).days
    rel = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days" if days > 1 else "now"
    return f"{d:%a %d %b %H:%M} ({rel})"


def _local(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch)


# ---------------------------------------------------------------- contests
@dataclass
class Contest:
    site: str
    key: str
    title: str
    start: datetime
    minutes: int
    url: str


def _cf_contests() -> list:
    out = []
    for c in _cf("contest.list", {"gym": "false"}):
        if c.get("phase") == "BEFORE" and c.get("startTimeSeconds"):
            out.append(Contest("codeforces", f"cf:{c['id']}", c.get("name", "Codeforces contest"), _local(c["startTimeSeconds"]),
                               int(c.get("durationSeconds", 0)) // 60, f"https://codeforces.com/contests/{c['id']}"))
    return out


def _lc_contests() -> list:
    data = _cached(("lc", "contests"), 600, lambda: _graphql(
        "query { upcomingContests { title titleSlug startTime duration } }"))
    return [Contest("leetcode", f"lc:{c['titleSlug']}", c["title"], _local(c["startTime"]), int(c.get("duration", 0)) // 60,
                    f"https://leetcode.com/contest/{c['titleSlug']}/")
            for c in data.get("upcomingContests") or [] if c.get("startTime")]


_AC_ROW = re.compile(
    r"<time class='fixtime fixtime-full'>([^<]+)</time>.*?href=\"/contests/([^\"]+)\">([^<]+)</a>.*?"
    r"<td class=\"text-center\">(\d+):(\d{2})</td>", re.S)


def _ac_contests() -> list:
    page = _cached(("ac", "contests"), 600, lambda: _get_text("https://atcoder.jp/contests?lang=en", "atcoder"))
    start = page.find('id="contest-table-upcoming"')
    if start < 0:
        return []
    end = page.find('id="contest-table-recent"', start)
    out = []
    for stamp, slug, title, hh, mm in _AC_ROW.findall(page[start:end if end > 0 else start + 20000]):
        try:
            when = datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M:%S%z").astimezone().replace(tzinfo=None)
        except ValueError:
            continue
        out.append(Contest("atcoder", f"ac:{slug}", html.unescape(title).strip(), when, int(hh) * 60 + int(mm),
                           f"https://atcoder.jp/contests/{slug}"))
    return out


_CONTEST_SOURCES = {"codeforces": _cf_contests, "leetcode": _lc_contests, "atcoder": _ac_contests}


def upcoming_contests(site: str = "", days: int = 7, now: Optional[datetime] = None) -> tuple:
    """([Contest] sorted by start within ``days``, [sentence for each site that failed])"""
    now = now or datetime.now()
    sites = [site] if site else list(SITES)
    found, failed = [], []

    def one(s: str):
        try:
            return s, [c for c in _CONTEST_SOURCES[s]() if now <= c.start <= now + timedelta(days=days)], ""
        except SiteError as e:
            return s, [], f"{_SITE_NAMES[s]}: {e}"
        except Exception as e:  # a page that changed shape must not take the answer down
            logger.warning("contest source %s failed: %s", s, e)
            return s, [], f"{_SITE_NAMES[s]}: its contest list could not be read"

    with ThreadPoolExecutor(max_workers=len(sites)) as pool:
        for _, rows, problem in pool.map(one, sites):  # in site order, so the failure sentences keep a stable order
            found.extend(rows)
            if problem:
                failed.append(problem)
    return sorted(found, key=lambda c: c.start), failed


def _contests(site: str, days: int) -> str:
    days = max(1, min(CONTESTS_MAX_DAYS, int(days or 7)))
    found, failed = upcoming_contests(site, days)
    now = datetime.now()
    scope = _SITE_NAMES[site] if site else "LeetCode, Codeforces and AtCoder"
    if not found:
        text = f"No {scope} contests start in the next {days} day{'s' if days != 1 else ''}."
    else:
        lines = [f"- {_when(c.start, now)} -- {_SITE_NAMES[c.site]}: {c.title}, {_duration(c.minutes)}. id {c.key}. {c.url}"
                 for c in found[:25]]
        text = f"{len(found)} contest{'s' if len(found) != 1 else ''} in the next {days} day{'s' if days != 1 else ''} (your local time):\n" + "\n".join(lines)
        if len(found) > 25:
            text += f"\n(and {len(found) - 25} more)"
    if failed:
        text += "\nCouldn't read: " + "; ".join(failed) + "."
    return text


# ---------------------------------------------------------------- LeetCode daily
def _lc_recent_ac(user: str) -> list:
    data = _cached(("lc", "recent", user), 300, lambda: _graphql(
        "query($u:String!,$l:Int){ recentAcSubmissionList(username:$u, limit:$l){ titleSlug } }", {"u": user, "l": 20}))
    return [s["titleSlug"] for s in data.get("recentAcSubmissionList") or []]


def _daily(user: str) -> str:
    data = _cached(("lc", "daily", datetime.now().strftime("%Y-%m-%d")), 3600, lambda: _graphql(
        "query { activeDailyCodingChallengeQuestion { date link question { questionFrontendId title titleSlug difficulty acRate "
        "topicTags { name } } } }"))
    d = data.get("activeDailyCodingChallengeQuestion")
    if not d or not d.get("question"):
        raise SiteError("LeetCode returned no daily problem")
    q = d["question"]
    topics = ", ".join(t["name"] for t in q.get("topicTags") or [])
    out = (f"LeetCode daily ({d.get('date', 'today')}): #{q.get('questionFrontendId')} {q['title']} -- {q.get('difficulty')}, "
           f"{_pct(q.get('acRate') or 0)} accepted" + (f"; topics: {topics}" if topics else "")
           + f". https://leetcode.com{d.get('link') or '/problems/' + q['titleSlug'] + '/'}")
    if user:
        try:
            out += (" You've already solved it." if q["titleSlug"] in _lc_recent_ac(user)
                    else " Not in your recent accepted submissions yet.")
        except SiteError:
            pass
    return out


# ---------------------------------------------------------------- profiles
def _lc_profile(user: str) -> str:
    data = _cached(("lc", "profile", user), 300, lambda: _graphql(
        "query($u:String!){ matchedUser(username:$u){ username profile{ ranking } submitStats: submitStatsGlobal "
        "{ acSubmissionNum { difficulty count } } userCalendar { streak totalActiveDays } } "
        "userContestRanking(username:$u){ rating attendedContestsCount topPercentage globalRanking } }", {"u": user}))
    m = data.get("matchedUser")
    if not m:
        raise SiteError(f"LeetCode has no public user '{user}'")
    counts = {x["difficulty"]: x["count"] for x in (m.get("submitStats") or {}).get("acSubmissionNum") or []}
    cal = m.get("userCalendar") or {}
    out = (f"LeetCode {m['username']}: {counts.get('All', 0)} solved (easy {counts.get('Easy', 0)}, medium {counts.get('Medium', 0)}, "
           f"hard {counts.get('Hard', 0)}); streak {cal.get('streak', 0)} day{'s' if cal.get('streak', 0) != 1 else ''}, "
           f"{cal.get('totalActiveDays', 0)} active days this year")
    rank = (m.get("profile") or {}).get("ranking")
    if rank:
        out += f"; site ranking #{rank:,}"
    cr = data.get("userContestRanking")
    if cr:
        out += (f". Contest rating {cr['rating']:.0f} over {cr['attendedContestsCount']} contest"
                f"{'s' if cr['attendedContestsCount'] != 1 else ''} (global #{cr['globalRanking']:,}, top {cr['topPercentage']}%)")
    else:
        out += ". No contests attended yet"
    return out + "."


def _cf_solved(handle: str) -> tuple:
    subs = _cf("user.status", {"handle": handle, "from": 1, "count": CF_SOLVED_WINDOW})
    solved = {(s["problem"].get("contestId"), s["problem"].get("index")) for s in subs if s.get("verdict") == "OK"}
    return solved, subs


def _cf_profile(handle: str) -> str:
    info = _cf("user.info", {"handles": handle})[0]
    out = f"Codeforces {info['handle']}: "
    if "rating" in info:
        out += (f"rating {info['rating']} ({info.get('rank', 'unrated')}), max {info.get('maxRating')} ({info.get('maxRank', '')})")
    else:
        out += "unrated"
    try:
        hist = _cf("user.rating", {"handle": handle})
    except SiteError:
        hist = []
    if hist:
        last = hist[-5:]
        trend = ", ".join(f"{h['newRating']} ({h['newRating'] - h['oldRating']:+d})" for h in last)
        out += f". {len(hist)} rated contest{'s' if len(hist) != 1 else ''}; last {len(last)}: {trend}"
    solved, subs = _cf_solved(handle)
    window = f" in the last {len(subs)} submissions" if len(subs) >= CF_SOLVED_WINDOW else ""
    week = sum(1 for s in subs if s.get("verdict") == "OK" and s.get("creationTimeSeconds", 0) > time.time() - 7 * 86400)
    out += f". {len(solved)} distinct problems solved{window}; {week} accepted submission{'s' if week != 1 else ''} in the last 7 days"
    return out + "."


def _ac_difficulty(raw: float) -> int:
    """AtCoder Problems clips low estimates: below 400 the shown value is 400 / e^((400 - d) / 400)."""
    return round(400 / math.exp((400 - raw) / 400)) if raw < 400 else round(raw)


def _ac_profile(user: str) -> str:
    out = f"AtCoder {user}: "
    try:
        hist = _cached(("ac", "history", user), 300, lambda: _get_json(f"https://atcoder.jp/users/{user}/history/json", "atcoder"))
    except SiteError as e:
        raise SiteError(f"no AtCoder history for '{user}' ({e})")
    rated = [h for h in hist if h.get("IsRated")]
    if rated:
        cur, top = rated[-1]["NewRating"], max(h["NewRating"] for h in rated)
        trend = ", ".join(f"{h['NewRating']} ({h['NewRating'] - h['OldRating']:+d})" for h in rated[-5:])
        out += f"rating {cur}, highest {top}; {len(rated)} rated contests; last {min(5, len(rated))}: {trend}"
    else:
        out += "no rated contests yet"
    try:
        info = _cached(("ac", "info", user), 300, lambda: _get_json(
            "https://kenkoooo.com/atcoder/atcoder-api/v3/user_info", "atcoder", {"user": user}))
        out += f". {info.get('accepted_count', 0)} problems accepted (rank #{info.get('accepted_count_rank', '?')})"
    except SiteError:
        pass
    return out + "."


_PROFILES = {"leetcode": _lc_profile, "codeforces": _cf_profile, "atcoder": _ac_profile}


def _profile(site: str, user: str) -> str:
    if user.strip() and not site:   # the same handle can belong to someone else on another site: ask, don't guess
        return f"Error: which site is '{user.strip()}' on? Say leetcode, codeforces or atcoder."
    targets = [site] if site else [s for s in SITES if _handle(s, "")]
    if not targets:
        return ("Error: no handles are saved. Ask the user for their handle on LeetCode, Codeforces or AtCoder "
                "(user='...', site='...'), or they can set LEETCODE_USERNAME, CODEFORCES_HANDLE, ATCODER_USER in backend/.env.")
    lines = []
    for s in targets:
        h = _handle(s, user if site else "")
        if not h:
            return _no_handle(s)
        try:
            lines.append(_PROFILES[s](h))
        except SiteError as e:
            lines.append(f"{_SITE_NAMES[s]}: {e}.")
    return "\n".join(lines)


# ---------------------------------------------------------------- practice picks
_LEVELS = {"easy": ("Easy", (0, 1200), (0, 400)), "medium": ("Medium", (1300, 1800), (400, 1200)),
           "hard": ("Hard", (1900, 4000), (1200, 4000))}   # (leetcode, codeforces rating, atcoder difficulty)
_RANGE = re.compile(r"^\s*(\d{3,4})\s*(?:-|–|to)\s*(\d{3,4})\s*$")
_ALIASES = {
    "leetcode": {"dp": "dynamic-programming", "dynamic programming": "dynamic-programming", "graph": "graph", "graphs": "graph",
                 "dfs": "depth-first-search", "bfs": "breadth-first-search", "binary search": "binary-search", "bst": "binary-search-tree",
                 "linked list": "linked-list", "hash": "hash-table", "hashing": "hash-table", "heap": "heap-priority-queue",
                 "priority queue": "heap-priority-queue", "trees": "tree", "strings": "string", "arrays": "array", "math": "math",
                 "two pointers": "two-pointers", "sliding window": "sliding-window", "bit manipulation": "bit-manipulation",
                 "backtracking": "backtracking", "greedy": "greedy", "stack": "stack", "queue": "queue", "trie": "trie",
                 "union find": "union-find", "sorting": "sorting", "recursion": "recursion", "topological sort": "topological-sort"},
    "codeforces": {"dynamic programming": "dp", "graph": "graphs", "tree": "trees", "binary search": "binary search", "math": "math",
                   "number theory": "number theory", "string": "strings", "hash": "hashing", "geometry": "geometry", "greedy": "greedy",
                   "dfs": "dfs and similar", "bfs": "dfs and similar", "bit manipulation": "bitmasks", "brute force": "brute force",
                   "implementation": "implementation", "constructive": "constructive algorithms", "sortings": "sortings",
                   "sorting": "sortings", "two pointers": "two pointers", "data structures": "data structures",
                   "shortest paths": "shortest paths", "dsu": "dsu", "union find": "dsu", "combinatorics": "combinatorics"},
}


def _level(site: str, level: str) -> tuple:
    """(leetcode difficulty word | None, low, high) for the level words or 'a-b' range; raises SiteError when unreadable."""
    text = str(level or "").strip().lower()
    if not text:
        return None, None, None
    m = _RANGE.match(text)
    if m:
        lo, hi = sorted((int(m.group(1)), int(m.group(2))))
        return None, lo, hi
    if text.isdigit():
        n = int(text)
        return None, max(0, n - 100), n + 100
    if text in _LEVELS:
        word, cf, ac = _LEVELS[text]
        lo, hi = cf if site == "codeforces" else ac
        return word, lo, hi
    raise SiteError(f"level '{level}' isn't easy, medium, hard or a range like 1200-1600")


def _pick_lines(rows: list) -> str:
    return "\n".join(f"- {r}" for r in rows)


def _pick_lc(topic: str, level: str, user: str) -> str:
    word, _, _ = _level("leetcode", level)
    slug = ""
    if topic.strip():
        t = topic.strip().lower()
        slug = _ALIASES["leetcode"].get(t, re.sub(r"[^a-z0-9]+", "-", t).strip("-"))
    flt: dict = {}
    if word:
        flt["difficulty"] = word.upper()
    if slug:
        flt["tags"] = [slug]
    query = ("query($c:String!,$l:Int,$s:Int,$f:QuestionListFilterInput){ problemsetQuestionList: questionList(categorySlug:$c, "
             "limit:$l, skip:$s, filters:$f){ total: totalNum questions: data { frontendQuestionId: questionFrontendId title "
             "titleSlug difficulty acRate paidOnly: isPaidOnly topicTags{ name } } } }")
    first = _graphql(query, {"c": "", "l": 1, "s": 0, "f": flt}).get("problemsetQuestionList") or {}
    total = first.get("total") or 0
    if total == 0:
        raise SiteError(f"no LeetCode problems match topic '{topic}'" + (f" at level {level}" if level else "")
                        + " (LeetCode topic slugs look like 'dynamic-programming', 'graph', 'binary-search')")
    skip = _rng.randint(0, max(0, total - 50))
    rows = (_graphql(query, {"c": "", "l": 50, "s": skip, "f": flt}).get("problemsetQuestionList") or {}).get("questions") or []
    solved = set()
    if user:
        try:
            solved = set(_lc_recent_ac(user))
        except SiteError:
            pass
    pool = [q for q in rows if not q.get("paidOnly") and q["titleSlug"] not in solved] or [q for q in rows if not q.get("paidOnly")]
    picks = _rng.sample(pool, min(PICK_COUNT, len(pool)))
    lines = [f"{q['frontendQuestionId']}. {q['title']} -- {q['difficulty']}, {_pct(q.get('acRate') or 0)} accepted; "
             f"topics: {', '.join(t['name'] for t in q.get('topicTags') or [])}. https://leetcode.com/problems/{q['titleSlug']}/"
             for q in picks]
    note = (f"\n(Skipped the {len(solved)} problems in your last accepted submissions; LeetCode's public data lists no more than 20, "
            "so it may still be one you solved earlier.)" if user else "")
    return f"{total} LeetCode problems match. Try:\n{_pick_lines(lines)}{note}"


def _pick_cf(topic: str, level: str, user: str) -> str:
    _, lo, hi = _level("codeforces", level)
    t = topic.strip().lower()
    tag = _ALIASES["codeforces"].get(t, t)
    data = _cf("problemset.problems", {"tags": tag} if tag else None)
    stats = {(s["contestId"], s["index"]): s.get("solvedCount", 0) for s in data.get("problemStatistics", [])}
    rated = [p for p in data["problems"] if p.get("rating")]
    if lo is not None:
        rated = [p for p in rated if lo <= p["rating"] <= hi]
    if not rated:
        raise SiteError(f"no Codeforces problems match tag '{topic}'" + (f" at level {level}" if level else "")
                        + " (tags look like 'dp', 'graphs', 'greedy', 'binary search', 'math', 'implementation')")
    solved, kept = set(), rated
    if user:
        try:
            solved, _ = _cf_solved(user)
            kept = [p for p in rated if (p["contestId"], p["index"]) not in solved] or rated
        except SiteError:
            pass
    picks = _rng.sample(kept, min(PICK_COUNT, len(kept)))
    lines = [f"{p['contestId']}{p['index']} {p['name']} -- rating {p['rating']}, {stats.get((p['contestId'], p['index']), 0):,} solved; "
             f"tags: {', '.join(p.get('tags', []))}. https://codeforces.com/problemset/problem/{p['contestId']}/{p['index']}"
             for p in picks]
    note = f"\n(Skipped {len(solved)} problems {user} has solved, from their last {CF_SOLVED_WINDOW} submissions.)" if user else ""
    return f"{len(rated)} Codeforces problems match. Try:\n{_pick_lines(lines)}{note}"


def _ac_solved(user: str) -> tuple:
    """(set of problem ids with an AC, True when the history was longer than AC_PAGES pages and was cut)"""
    def fetch():
        seen, since, cut = set(), 0, False
        for page_no in range(AC_PAGES):
            page = _get_json("https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions", "atcoder",
                             {"user": user, "from_second": since})
            if not page:
                break
            seen.update(s["problem_id"] for s in page if s.get("result") == "AC")
            if len(page) < 500:
                break
            since = max(s["epoch_second"] for s in page) + 1
            cut = page_no == AC_PAGES - 1
        return seen, cut
    return _cached(("ac", "solved", user), 300, fetch)


def _ac_contest(problem_id: str, fallback: str) -> str:
    """The contest a task was first set in: 'abc295_b' -> 'abc295' (problems.json also lists the practice contests that reuse it)."""
    head, _, tail = problem_id.rpartition("_")
    return head if head and tail else fallback


def _pick_ac(topic: str, level: str, user: str) -> str:
    _, lo, hi = _level("atcoder", level)
    problems = _cached(("ac", "problems"), 3600, lambda: _get_json("https://kenkoooo.com/atcoder/resources/problems.json", "atcoder"))
    models = _cached(("ac", "models"), 3600, lambda: _get_json("https://kenkoooo.com/atcoder/resources/problem-models.json", "atcoder"))
    rows = []
    for p in problems:
        m = models.get(p["id"])
        if m and m.get("difficulty") is not None and not m.get("is_experimental"):
            rows.append((p, _ac_difficulty(m["difficulty"])))
    if lo is not None:
        rows = [(p, d) for p, d in rows if lo <= d <= hi]
    if not rows:
        raise SiteError("no AtCoder problems in that difficulty range")
    solved, cut = set(), False
    if user:
        try:
            solved, cut = _ac_solved(user)
        except SiteError:
            pass
    pool = [r for r in rows if r[0]["id"] not in solved] or rows
    picks = _rng.sample(pool, min(PICK_COUNT, len(pool)))
    lines = [f"{p['title']} ({_ac_contest(p['id'], p['contest_id'])}) -- difficulty ~{d}. "
             f"https://atcoder.jp/contests/{_ac_contest(p['id'], p['contest_id'])}/tasks/{p['id']}" for p, d in picks]
    notes = []
    if topic.strip():
        notes.append(f"AtCoder has no topic tags, so '{topic}' was ignored")
    if user:
        notes.append(f"skipped {len(solved)} problems {user} has solved"
                     + (f", from the first {AC_PAGES * 500} submissions of a longer history" if cut else ""))
    return f"{len(rows)} AtCoder problems match. Try:\n{_pick_lines(lines)}" + (f"\n({'; '.join(notes)}.)" if notes else "")


_PICKS = {"leetcode": _pick_lc, "codeforces": _pick_cf, "atcoder": _pick_ac}


def _pick(site: str, user: str, topic: str, level: str) -> str:
    if not site:
        site = next((s for s in SITES if _handle(s, "")), "leetcode")
    return _PICKS[site](topic, level, _handle(site, user))


# ---------------------------------------------------------------- a contest onto the calendar
def _schedule(site: str, contest: str, session_id: Optional[str]) -> str:
    words = " ".join(str(contest or "").lower().split())
    if not words:
        return "Error: schedule needs contest (its id from the contests list, or some words of its name)."
    found, failed = upcoming_contests(site, CONTESTS_MAX_DAYS)
    exact = [c for c in found if c.key.lower() == words]
    hits = exact or [c for c in found if all(w in f"{c.title} {c.site}".lower() for w in words.split())]
    if not hits:
        return (f"Error: no upcoming contest matches '{contest}'. List them first with action 'contests'."
                + (" Couldn't read: " + "; ".join(failed) + "." if failed else ""))
    if len(hits) > 1:
        return ("Error: that matches more than one contest; use an id: "
                + "; ".join(f"{c.key} ({_SITE_NAMES[c.site]}: {c.title}, {c.start:%a %d %b %H:%M})" for c in hits[:6]) + ".")
    c = hits[0]
    from app.agent.tools.calendar_events import calendar
    from app.agent.tools.reminders import remind_me

    title = f"{_SITE_NAMES[c.site]}: {c.title}"
    added = calendar(action="add", title=title, when=f"{c.start:%Y-%m-%d %H:%M}", duration=f"{max(1, c.minutes)}m", session_id=session_id)
    if added.startswith(("Error", "Not saved")):
        return added
    if added.startswith("Already"):
        return f"{added} (No second reminder was set.)"
    remind_at = c.start - REMINDER_LEAD
    if remind_at <= datetime.now():
        return f"{added} It starts too soon for a reminder."
    reminder = remind_me(text=f"{c.title} starts in {int(REMINDER_LEAD.total_seconds() // 60)} minutes",
                         when=f"{remind_at:%Y-%m-%d %H:%M}")
    return f"{added} {reminder}"


# ---------------------------------------------------------------- entry point
def _days(query: str) -> int:
    """'14' or '14 days' -> 14; anything else ('Div 2', 'ABC 483') is not a day count: 7."""
    m = re.fullmatch(r"\s*(\d{1,3})\s*(?:days?|d)?\s*", str(query or ""), re.IGNORECASE)
    return int(m.group(1)) if m else 7


_LEVEL_WORDS = {"easy": "easy", "medium": "medium", "med": "medium", "hard": "hard"}


def _split_query(query: str) -> tuple:
    """'dp medium' -> ('dp', 'medium'); 'graph 1200-1600' -> ('graph', '1200-1600'); 'hard' -> ('', 'hard')."""
    text = " ".join(str(query or "").split())
    m = re.search(r"\b(\d{3,4})\s*(?:-|–|to)\s*(\d{3,4})\b", text)
    level = ""
    if m:
        level, text = f"{m.group(1)}-{m.group(2)}", (text[:m.start()] + " " + text[m.end():])
    else:
        words = text.split()
        for i, w in enumerate(words):
            if w.lower() in _LEVEL_WORDS or re.fullmatch(r"\d{3,4}", w):   # 'easy', or a lone rating: 'dp 1400'
                level = _LEVEL_WORDS.get(w.lower(), w)
                text = " ".join(words[:i] + words[i + 1:])
                break
    return " ".join(text.replace(",", " ").split()), level


def coding(action: str = "contests", site: str = "", user: str = "", query: str = "",
           session_id: Optional[str] = None) -> str:
    """
    LeetCode, Codeforces and AtCoder: contests, today's LeetCode daily, a handle's rating and solved counts, a practice problem, or a contest added to the calendar with a reminder.

    Args:
        action: contests, daily, profile, pick or schedule.
        site: leetcode, codeforces or atcoder; empty = all sites (contests) or the user's saved one.
        user: A handle; empty = the saved one.
        query: contests: how many days ahead (default 7). pick: topic and level, e.g. 'dp medium', 'graph 1200-1600'.
            schedule: the contest's id or name words.
    """
    act = str(action or "contests").strip().lower()
    if act not in ("contests", "daily", "profile", "pick", "schedule"):
        return "Error: action must be contests, daily, profile, pick or schedule."
    s = _site(site)
    if s and s not in SITES:
        return f"Error: site must be leetcode, codeforces or atcoder, not '{site}'."
    try:
        if act == "contests":
            return _contests(s, _days(query))
        if act == "daily":
            return _daily(_handle("leetcode", user if s in ("", "leetcode") else ""))
        if act == "profile":
            return _profile(s, user)
        if act == "pick":
            topic, level = _split_query(query)
            return _pick(s, user, topic, level)
        return _schedule(s, query, session_id)
    except SiteError as e:
        return f"Error: {e}."
    except Exception as e:
        logger.exception("coding(%s) failed", act)
        return f"Error: the coding sites could not be read ({type(e).__name__}: {e})."
