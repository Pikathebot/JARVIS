"""The `coding` tool: LeetCode / Codeforces / AtCoder contests, daily, profile, pick, schedule (PLAN 4.23b).

Fixtures have the shapes the live services returned on 2026-11-03 (checked by hand with curl); every time in them is
computed from "now", so nothing here depends on the date the suite runs.
"""
import random
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.agent.tools import coding as cp
from app.agent.tools.coding import coding
from app.config import settings


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    cp._cache.clear()
    monkeypatch.setattr(cp, "_cf_wait", lambda: None)
    monkeypatch.setattr(cp, "_rng", random.Random(7))
    for name in ("codeforces_handle", "leetcode_username", "atcoder_user"):
        monkeypatch.setattr(settings, name, "")
    yield
    cp._cache.clear()


def _in(days=0, hours=0):
    return datetime.now() + timedelta(days=days, hours=hours)


def _epoch(d: datetime) -> int:
    return int(d.timestamp())


# ---------------------------------------------------------------- fake sites
class Sites:
    """Routes the tool's three network helpers to canned data; records what was asked."""

    def __init__(self, monkeypatch):
        self.json: dict = {}      # url -> data (or an exception to raise)
        self.text: dict = {}
        self.graphql: list = []   # [(substring of the query, data or exception)]
        self.calls: list = []
        monkeypatch.setattr(cp, "_get_json", self._get_json)
        monkeypatch.setattr(cp, "_get_text", self._get_text)
        monkeypatch.setattr(cp, "_graphql", self._graphql)

    def _get_json(self, url, site, params=None):
        self.calls.append(("json", url, params))
        data = self.json[url]
        if isinstance(data, Exception):
            raise data
        return data(params) if callable(data) else data

    def _get_text(self, url, site):
        self.calls.append(("text", url, None))
        data = self.text[url]
        if isinstance(data, Exception):
            raise data
        return data

    def _graphql(self, query, variables=None):
        self.calls.append(("graphql", query, variables))
        for needle, data in self.graphql:
            if needle in query:
                if isinstance(data, Exception):
                    raise data
                return data(variables) if callable(data) else data
        raise AssertionError(f"unexpected GraphQL query: {query[:80]}")


@pytest.fixture
def sites(monkeypatch):
    return Sites(monkeypatch)


CF = "https://codeforces.com/api/"


def cf_ok(result):
    return {"status": "OK", "result": result}


def cf_contest(cid, name, start, minutes=120, phase="BEFORE"):
    return {"id": cid, "name": name, "type": "CF", "phase": phase, "frozen": False, "durationSeconds": minutes * 60,
            "startTimeSeconds": _epoch(start), "relativeTimeSeconds": -100}


def ac_page(rows):
    """The shape of atcoder.jp/contests: an upcoming table, then the recent one."""
    body = "".join(
        "<tr><td class=\"text-center\"><a href='http://www.timeanddate.com/worldclock/fixedtime.html?iso=x&p1=248' target='blank'>"
        f"<time class='fixtime fixtime-full'>{d.astimezone(timezone(timedelta(hours=9))):%Y-%m-%d %H:%M:%S%z}</time></a></td>"
        f"<td ><span title=\"Algorithm\">A</span><span class=\"user-blue\">o</span> <a href=\"/contests/{slug}\">{title}</a></td>"
        f"<td class=\"text-center\">{mm // 60:02d}:{mm % 60:02d}</td><td class=\"text-center\"> - 1999</td></tr>"
        for slug, title, d, mm in rows)
    return ('<div id="contest-table-action"></div><div id="contest-table-upcoming"><h3>Upcoming Contests</h3><table><thead><tr>'
            f"<th>Start</th></tr></thead><tbody>{body}</tbody></table></div>"
            '<div id="contest-table-recent"><h3>Recent Contests</h3><table><tbody><tr><td><time class=\'fixtime fixtime-full\'>2020-01-01 21:00:00+0900</time></td>'
            '<td><a href="/contests/old1">Old Contest</a></td><td class="text-center">01:40</td></tr></tbody></table></div>')


def lc_contests(*rows):
    return {"upcomingContests": [{"title": t, "titleSlug": s, "startTime": _epoch(d), "duration": 5400} for t, s, d in rows]}


def all_sites(sites):
    sites.json[CF + "contest.list"] = cf_ok([
        cf_contest(2283, "Educational Codeforces Round 196", _in(2)),
        cf_contest(2284, "Codeforces Round (Div. 2)", _in(40)),           # beyond a week
        cf_contest(2200, "Finished Round", _in(-3), phase="FINISHED"),
        cf_contest(2201, "Running Round", _in(0, -1), phase="CODING"),
    ])
    sites.graphql.append(("upcomingContests", lc_contests(("Weekly Contest 527", "weekly-contest-527", _in(1)))))
    sites.text["https://atcoder.jp/contests?lang=en"] = ac_page([("abc483", "AtCoder Beginner Contest 483", _in(3), 100)])


# ---------------------------------------------------------------- contests
def test_contests_merge_the_three_sites_in_start_order_within_the_window(sites):
    all_sites(sites)
    out = coding(action="contests")
    lines = [l for l in out.splitlines() if l.startswith("- ")]
    assert out.startswith("3 contests in the next 7 days (your local time):")
    assert [("LeetCode" in l, "Codeforces" in l, "AtCoder" in l) for l in lines] == [
        (True, False, False), (False, True, False), (False, False, True)]          # in 1, 2 and 3 days
    assert "tomorrow" in lines[0] and "in 2 days" in lines[1] and "in 3 days" in lines[2]
    assert "id lc:weekly-contest-527" in lines[0] and "id cf:2283" in lines[1] and "id ac:abc483" in lines[2]
    assert "https://codeforces.com/contests/2283" in lines[1] and "https://atcoder.jp/contests/abc483" in lines[2]
    # eval 11-05 case 21: the model put a round on the calendar by hand (1 h, no reminder); the list points at schedule.
    assert "action=schedule" in out.splitlines()[-1]
    assert "Educational Codeforces Round 196, 2h." in lines[1] and "AtCoder Beginner Contest 483, 1h40." in lines[2]
    assert "Finished Round" not in out and "Running Round" not in out


def test_contests_days_come_from_the_query_and_a_site_narrows_it(sites):
    all_sites(sites)
    assert "Codeforces Round (Div. 2)" in coding(action="contests", site="codeforces", query="60")
    out = coding(action="contests", site="cf")
    assert "1 contest in the next 7 days" in out and "LeetCode" not in out
    assert coding(action="contests", site="atcoder", query="1").startswith("No AtCoder contests start in the next 1 day.")


def test_one_site_down_does_not_hide_the_others(sites):
    all_sites(sites)
    sites.graphql[0] = ("upcomingContests", cp.SiteError("LeetCode answered HTTP 503"))
    out = coding(action="contests")
    assert "2 contests" in out and "Educational Codeforces Round 196" in out
    assert out.rstrip().endswith("Couldn't read: LeetCode: LeetCode answered HTTP 503.")


def test_an_atcoder_page_that_changed_shape_yields_nothing_not_a_crash(sites):
    all_sites(sites)
    sites.text["https://atcoder.jp/contests?lang=en"] = "<html><body>redesigned</body></html>"
    out = coding(action="contests", site="atcoder")
    assert out.startswith("No AtCoder contests")


def test_results_are_cached_so_a_follow_up_does_not_ask_the_sites_again(sites):
    all_sites(sites)
    coding(action="contests")
    n = len(sites.calls)
    coding(action="contests", query="3")
    assert len(sites.calls) == n


# ---------------------------------------------------------------- daily
def lc_daily(slug="two-sum", title="Two Sum"):
    return {"activeDailyCodingChallengeQuestion": {"date": "2026-11-03", "link": f"/problems/{slug}/", "question": {
        "questionFrontendId": "1", "title": title, "titleSlug": slug, "difficulty": "Easy", "acRate": 55.4,
        "topicTags": [{"name": "Array"}, {"name": "Hash Table"}]}}}


def test_the_daily_says_the_problem_and_whether_the_handle_solved_it(sites, monkeypatch):
    sites.graphql.append(("activeDailyCodingChallengeQuestion", lc_daily()))
    out = coding(action="daily")
    assert out == ("LeetCode daily (2026-11-03): #1 Two Sum -- Easy, 55% accepted; topics: Array, Hash Table. "
                   "https://leetcode.com/problems/two-sum/")
    monkeypatch.setattr(settings, "leetcode_username", "lee215")
    sites.graphql.append(("recentAcSubmissionList", {"recentAcSubmissionList": [{"titleSlug": "two-sum"}]}))
    assert coding(action="daily").endswith("You've already solved it.")
    cp._cache.clear()
    sites.graphql[-1] = ("recentAcSubmissionList", {"recentAcSubmissionList": [{"titleSlug": "other"}]})
    assert coding(action="daily").endswith("Not in your recent accepted submissions yet.")


def test_a_daily_the_site_does_not_return_is_an_error_sentence(sites):
    sites.graphql.append(("activeDailyCodingChallengeQuestion", {"activeDailyCodingChallengeQuestion": None}))
    assert coding(action="daily") == "Error: LeetCode returned no daily problem."


# ---------------------------------------------------------------- profile
def lc_user(name="lee215"):
    return {"matchedUser": {"username": name, "profile": {"ranking": 102193},
                            "submitStats": {"acSubmissionNum": [{"difficulty": "All", "count": 695}, {"difficulty": "Easy", "count": 127},
                                                                 {"difficulty": "Medium", "count": 416}, {"difficulty": "Hard", "count": 152}]},
                            "userCalendar": {"streak": 3, "totalActiveDays": 48}},
            "userContestRanking": {"rating": 2204.316, "attendedContestsCount": 27, "topPercentage": 0.9, "globalRanking": 7625}}


def test_leetcode_profile(sites):
    sites.graphql.append(("matchedUser", lc_user()))
    out = coding(action="profile", site="leetcode", user="lee215")
    assert out == ("LeetCode lee215: 695 solved (easy 127, medium 416, hard 152); streak 3 days, 48 active days this year; "
                   "site ranking #102,193. Contest rating 2204 over 27 contests (global #7,625, top 0.9%).")


def test_leetcode_unknown_user_and_no_contests(sites):
    sites.graphql.append(("matchedUser", {"matchedUser": None, "userContestRanking": None}))
    assert coding(action="profile", site="leetcode", user="nobody") == "LeetCode: LeetCode has no public user 'nobody'."
    cp._cache.clear()
    data = lc_user()
    data["userContestRanking"] = None
    sites.graphql[0] = ("matchedUser", data)
    assert "No contests attended yet." in coding(action="profile", site="leetcode", user="lee215")


def cf_user():
    return cf_ok([{"handle": "tourist", "rating": 3382, "maxRating": 4009, "rank": "legendary grandmaster", "maxRank": "tourist"}])


def cf_history():
    return cf_ok([{"contestId": i, "oldRating": 3300 + i, "newRating": 3310 + i * 2} for i in range(1, 7)])


def cf_status(n_ok_recent=2):
    now = int(time.time())
    subs = [{"verdict": "OK", "creationTimeSeconds": now - 3600 * (i + 1), "problem": {"contestId": 100 + i, "index": "A"}} for i in range(n_ok_recent)]
    subs += [{"verdict": "OK", "creationTimeSeconds": now - 30 * 86400, "problem": {"contestId": 100, "index": "A"}},          # same problem again
             {"verdict": "WRONG_ANSWER", "creationTimeSeconds": now - 40, "problem": {"contestId": 999, "index": "Z"}},
             {"verdict": "OK", "creationTimeSeconds": now - 90 * 86400, "problem": {"contestId": 50, "index": "B"}}]
    return cf_ok(subs)


def test_codeforces_profile_rating_trend_and_distinct_solves(sites):
    sites.json[CF + "user.info"] = cf_user()
    sites.json[CF + "user.rating"] = cf_history()
    sites.json[CF + "user.status"] = cf_status()
    out = coding(action="profile", site="codeforces", user="tourist")
    assert out.startswith("Codeforces tourist: rating 3382 (legendary grandmaster), max 4009 (tourist). 6 rated contests; last 5: ")
    assert "last 5: 3314 (+12), 3316 (+13), 3318 (+14), 3320 (+15), 3322 (+16)." in out   # contests 2-6 of 6; the first is dropped
    assert "3 distinct problems solved; 2 accepted submissions in the last 7 days." in out   # 100A twice counts once; the WA is not a solve


def test_atcoder_profile(sites):
    sites.json["https://atcoder.jp/users/chokudai/history/json"] = [
        {"IsRated": True, "OldRating": 0, "NewRating": 1255}, {"IsRated": False, "OldRating": 1255, "NewRating": 1255},
        {"IsRated": True, "OldRating": 1255, "NewRating": 1100}, {"IsRated": True, "OldRating": 1100, "NewRating": 1500}]
    sites.json["https://kenkoooo.com/atcoder/atcoder-api/v3/user_info"] = {"user_id": "chokudai", "accepted_count": 894, "accepted_count_rank": 5770}
    out = coding(action="profile", site="atcoder", user="chokudai")
    assert out == ("AtCoder chokudai: rating 1500, highest 1500; 3 rated contests; last 3: 1255 (+1255), 1100 (-155), 1500 (+400). "
                   "894 problems accepted (rank #5770).")


def test_profile_without_any_handle_asks_for_one(sites):
    out = coding(action="profile")
    assert out.startswith("Error: no handles are saved. Ask the user") and "CODEFORCES_HANDLE" in out
    assert coding(action="profile", site="codeforces").startswith("Error: no Codeforces handle.")


def test_profile_uses_every_saved_handle_when_no_site_is_named(sites, monkeypatch):
    monkeypatch.setattr(settings, "leetcode_username", "lee215")
    monkeypatch.setattr(settings, "atcoder_user", "chokudai")
    sites.graphql.append(("matchedUser", lc_user()))
    sites.json["https://atcoder.jp/users/chokudai/history/json"] = []
    sites.json["https://kenkoooo.com/atcoder/atcoder-api/v3/user_info"] = cp.SiteError("down")
    out = coding(action="profile").splitlines()
    assert out[0].startswith("LeetCode lee215:") and out[1] == "AtCoder chokudai: no rated contests yet."


@pytest.mark.parametrize("bad", ["a b", "x/../y", "tourist;drop", "z" * 41, "é"])
def test_a_handle_that_could_alter_a_url_is_refused(sites, bad):
    assert "doesn't look like a Codeforces handle" in coding(action="profile", site="codeforces", user=bad)


# ---------------------------------------------------------------- pick
def lc_questions(rows, total=None):
    qs = [{"frontendQuestionId": str(i), "title": t, "titleSlug": s, "difficulty": d, "acRate": 50.0, "paidOnly": paid,
           "topicTags": [{"name": "Graph Theory"}]} for i, (t, s, d, paid) in enumerate(rows, 1)]
    return {"problemsetQuestionList": {"total": total if total is not None else len(qs), "questions": qs}}


def test_leetcode_pick_filters_by_alias_and_difficulty_and_skips_paid_and_recent(sites, monkeypatch):
    monkeypatch.setattr(settings, "leetcode_username", "lee215")
    rows = [(f"Problem {i}", f"p{i}", "Medium", i == 1) for i in range(1, 8)]            # p1 is paid-only
    sites.graphql.append(("problemsetQuestionList", lambda v: lc_questions(rows, 7 if v["l"] == 1 else None)))
    sites.graphql.append(("recentAcSubmissionList", {"recentAcSubmissionList": [{"titleSlug": "p2"}, {"titleSlug": "p3"}]}))
    out = coding(action="pick", site="leetcode", query="dp medium")
    asked = [c[2] for c in sites.calls if c[0] == "graphql" and "problemsetQuestionList" in c[1]]
    assert asked[0]["f"] == {"difficulty": "MEDIUM", "tags": ["dynamic-programming"]}
    assert out.startswith("7 LeetCode problems match. Try:")
    picked = [l for l in out.splitlines() if l.startswith("- ")]
    assert len(picked) == 3 and all("Problem 1 " not in l and "Problem 2 " not in l and "Problem 3 " not in l for l in picked)
    assert all("https://leetcode.com/problems/p" in l and "Medium" in l for l in picked)
    assert "Skipped the 2 problems in your last accepted submissions" in out and "no more than 20" in out


def test_leetcode_pick_with_no_match_explains_the_topic_slugs(sites):
    sites.graphql.append(("problemsetQuestionList", lc_questions([], total=0)))
    out = coding(action="pick", site="leetcode", query="underwater basket weaving hard")
    assert out.startswith("Error: no LeetCode problems match topic 'underwater basket weaving' at level hard")
    assert "'dynamic-programming'" in out
    assert sites.calls[0][2]["f"] == {"difficulty": "HARD", "tags": ["underwater-basket-weaving"]}


def cf_problems():
    probs = [{"contestId": 10 + i, "index": "A", "name": f"CF {i}", "rating": r, "tags": ["dp"]}
             for i, r in enumerate([800, 1200, 1500, 1700, 1800, 2000, 2400])]
    probs.append({"contestId": 99, "index": "Z", "name": "Unrated", "tags": ["dp"]})
    return cf_ok({"problems": probs, "problemStatistics": [{"contestId": p["contestId"], "index": p["index"], "solvedCount": 1234} for p in probs]})


def test_codeforces_pick_level_tag_alias_and_solved_exclusion(sites, monkeypatch):
    seen = {}
    sites.json[CF + "problemset.problems"] = lambda params: seen.setdefault("p", params) and cf_problems()
    sites.json[CF + "user.status"] = cf_ok([{"verdict": "OK", "creationTimeSeconds": 1, "problem": {"contestId": 12, "index": "A"}}])
    out = coding(action="pick", site="codeforces", user="tourist", query="dynamic programming medium")
    assert seen["p"] == {"tags": "dp"}
    # medium = 1300-1800: CF 2 (1500), CF 3 (1700), CF 4 (1800) -- minus the one tourist solved (contest 12 = CF 2)
    picked = [l for l in out.splitlines() if l.startswith("- ")]
    assert out.startswith("3 Codeforces problems match. Try:") and len(picked) == 2
    assert {l.split(" ")[1] for l in picked} == {"13A", "14A"}              # contests 10+i: rating 1700 -> 13, 1800 -> 14
    assert all("https://codeforces.com/problemset/problem/" in l and "1,234 solved" in l and "tags: dp" in l for l in picked)
    assert "Skipped 1 problems tourist has solved" in out


def test_codeforces_pick_with_a_numeric_range_and_no_match(sites):
    sites.json[CF + "problemset.problems"] = cf_problems()
    out = coding(action="pick", site="codeforces", query="dp 2000-2500")
    assert {l.split(" -- ")[1].split(",")[0] for l in out.splitlines() if l.startswith("- ")} == {"rating 2000", "rating 2400"}
    assert coding(action="pick", site="codeforces", query="dp 3000-3500").startswith("Error: no Codeforces problems match tag 'dp' at level 3000-3500")


def test_atcoder_pick_clips_difficulty_ignores_topic_and_skips_solved(sites):
    problems = [{"id": f"abc{n}_a", "contest_id": f"abc{n}", "problem_index": "A", "name": "x", "title": f"A. Task {n}"} for n in (101, 102, 103, 104)]
    models = {"abc101_a": {"difficulty": -400},      # clipped: 400 / e^((400+400)/400) ~ 54
              "abc102_a": {"difficulty": 600}, "abc103_a": {"difficulty": 700}, "abc104_a": {"difficulty": 900, "is_experimental": True}}
    sites.json["https://kenkoooo.com/atcoder/resources/problems.json"] = problems
    sites.json["https://kenkoooo.com/atcoder/resources/problem-models.json"] = models
    sites.json["https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions"] = [
        {"problem_id": "abc102_a", "epoch_second": 5, "result": "AC"}, {"problem_id": "abc103_a", "epoch_second": 6, "result": "WA"}]
    out = coding(action="pick", site="atcoder", user="chokudai", query="graphs medium")
    picked = [l for l in out.splitlines() if l.startswith("- ")]
    assert out.startswith("2 AtCoder problems match. Try:")                 # difficulty 600 and 700 are medium; the experimental one is out
    assert len(picked) == 1 and "Task 103 (abc103)" in picked[0] and "difficulty ~700" in picked[0]
    assert "https://atcoder.jp/contests/abc103/tasks/abc103_a" in picked[0]
    assert "AtCoder has no topic tags, so 'graphs' was ignored" in out and "skipped 1 problems chokudai has solved" in out
    assert cp._ac_difficulty(-400) == 54 and cp._ac_difficulty(399) == 399 and cp._ac_difficulty(2000) == 2000


def test_atcoder_pick_names_the_original_contest_not_the_practice_one_that_reuses_the_task(sites):
    # seen live: problems.json lists abc295_b under adt_easy_20260731_2, whose own page is a practice set
    sites.json["https://kenkoooo.com/atcoder/resources/problems.json"] = [
        {"id": "abc295_b", "contest_id": "adt_easy_20260731_2", "problem_index": "C", "name": "Bombs", "title": "C. Bombs"}]
    sites.json["https://kenkoooo.com/atcoder/resources/problem-models.json"] = {"abc295_b": {"difficulty": 156}}
    out = coding(action="pick", site="atcoder", query="easy")
    assert "C. Bombs (abc295) -- difficulty ~217. https://atcoder.jp/contests/abc295/tasks/abc295_b" in out
    assert "adt_easy" not in out


def test_a_very_long_atcoder_history_says_it_was_cut(sites, monkeypatch):
    monkeypatch.setattr(cp, "AC_PAGES", 2)
    sites.json["https://kenkoooo.com/atcoder/resources/problems.json"] = [
        {"id": "abc1_a", "contest_id": "abc1", "problem_index": "A", "name": "x", "title": "A. X"}]
    sites.json["https://kenkoooo.com/atcoder/resources/problem-models.json"] = {"abc1_a": {"difficulty": 100}}
    full = [{"problem_id": f"old{i}_a", "epoch_second": i + 1, "result": "AC"} for i in range(500)]
    sites.json["https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions"] = lambda params: full
    out = coding(action="pick", site="atcoder", user="busy", query="easy")
    assert "from the first 1000 submissions of a longer history" in out


def test_codeforces_explains_an_unknown_handle_in_its_own_words(monkeypatch):
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, *a, **k):
            resp = httpx.Response(400, json={"status": "FAILED", "comment": "handles: User with handle nobody not found"},
                                  request=httpx.Request("GET", "https://codeforces.com/api/user.info"))
            return resp
    monkeypatch.setattr(cp, "_client", lambda: C())
    assert coding(action="profile", site="codeforces", user="nobody") == (
        "Codeforces: Codeforces said: handles: User with handle nobody not found.")


@pytest.mark.parametrize("query,expected", [
    ("dp medium", ("dp", "medium")), ("hard", ("", "hard")), ("graph 1200-1600", ("graph", "1200-1600")),
    ("binary search, easy", ("binary search", "easy")), ("2000 to 2400 greedy", ("greedy", "2000-2400")), ("", ("", "")),
    ("Medium DP", ("DP", "medium"))])
def test_the_pick_query_splits_into_topic_and_level(query, expected):
    assert cp._split_query(query) == expected


def test_a_level_nobody_can_read_is_an_error_sentence(sites):
    sites.json[CF + "problemset.problems"] = cf_problems()
    with pytest.raises(cp.SiteError, match="isn't easy, medium, hard or a range"):
        cp._level("codeforces", "impossible")
    assert cp._level("codeforces", "1400") == (None, 1300, 1500)


# ---------------------------------------------------------------- schedule
@pytest.fixture
def calendar_env(tmp_path, monkeypatch):
    from app.agent.tools import reminders
    from app.google import auth

    monkeypatch.setattr(auth, "DATA_DIR", tmp_path)
    made = []

    class Scheduler:
        def create(self, routine):
            made.append(routine)
            return routine

        def list(self):
            return made

    monkeypatch.setattr(reminders, "_scheduler", lambda: Scheduler())
    return made


def test_schedule_adds_the_calendar_event_in_local_time_and_a_reminder_15_minutes_before(sites, calendar_env):
    from app.agent.tools import calendar_events as cal

    all_sites(sites)
    out = coding(action="schedule", site="codeforces", query="cf:2283", session_id="s1")
    start = cp._cf_contests()[0].start
    assert out.startswith("Added ") and "Codeforces: Educational Codeforces Round 196" in out and "Reminder set" in out
    ev = cal.load()["events"][0]
    assert ev["title"] == "Codeforces: Educational Codeforces Round 196"
    assert ev["start"] == f"{start:%Y-%m-%dT%H:%M}" and ev["end"] == f"{start + timedelta(minutes=120):%Y-%m-%dT%H:%M}"
    assert len(calendar_env) == 1 and calendar_env[0].at == f"{start - timedelta(minutes=15):%Y-%m-%dT%H:%M}"
    assert calendar_env[0].message == "Educational Codeforces Round 196 starts in 15 minutes" and calendar_env[0].reminder


def test_scheduling_twice_does_not_stack_reminders(sites, calendar_env):
    all_sites(sites)
    coding(action="schedule", query="cf:2283")
    again = coding(action="schedule", query="cf:2283")
    assert again.startswith("Already on the calendar") and "No second reminder" in again
    assert len(calendar_env) == 1


def test_schedule_finds_a_contest_by_name_words_and_refuses_an_ambiguous_one(sites, calendar_env):
    all_sites(sites)
    assert coding(action="schedule", query="weekly 527").startswith("Added ")
    out = coding(action="schedule", query="contest")                       # "Weekly Contest 527" and "...Beginner Contest 483"
    assert out.startswith("Error: that matches more than one contest; use an id:") and "lc:weekly-contest-527" in out and "ac:abc483" in out
    assert coding(action="schedule", query="no such round").startswith("Error: no upcoming contest matches 'no such round'")
    assert coding(action="schedule").startswith("Error: schedule needs contest")


def test_schedule_in_an_off_the_record_chat_saves_nothing(sites, calendar_env, monkeypatch):
    from app.agent.tools import calendar_events as cal

    all_sites(sites)
    monkeypatch.setattr(cal, "_is_ephemeral", lambda sid: sid == "off")
    out = coding(action="schedule", query="cf:2283", session_id="off")
    assert out.startswith("Not saved: this is an off-the-record") and calendar_env == []


def test_a_contest_starting_in_ten_minutes_gets_the_event_but_no_reminder(sites, calendar_env):
    sites.json["https://codeforces.com/api/contest.list"] = cf_ok([cf_contest(5, "Soon Round", _in(0, 0) + timedelta(minutes=10))])
    out = coding(action="schedule", site="codeforces", query="cf:5")
    assert out.startswith("Added ") and out.endswith("It starts too soon for a reminder.") and calendar_env == []


# ---------------------------------------------------------------- errors and plumbing
@pytest.mark.parametrize("exc,sentence", [
    (httpx.ReadTimeout("slow"), "Codeforces did not answer in time"),
    (httpx.ConnectError("no route"), "couldn't reach Codeforces (ConnectError)")])
def test_network_failures_read_as_sentences(monkeypatch, exc, sentence):
    class Boom:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, *a, **k): raise exc
    monkeypatch.setattr(cp, "_client", lambda: Boom())
    assert coding(action="contests", site="codeforces").endswith(f"Couldn't read: Codeforces: {sentence}.")


def test_an_http_error_status_is_named(monkeypatch):
    class Resp:
        status_code = 503
        def raise_for_status(self):
            raise httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://x"), response=httpx.Response(503))
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, *a, **k): return Resp()
    monkeypatch.setattr(cp, "_client", lambda: C())
    assert "Codeforces answered HTTP 503" in coding(action="contests", site="codeforces")


def test_bad_arguments(sites):
    assert coding(action="solve").startswith("Error: action must be contests, daily, profile, pick or schedule.")
    assert coding(action="contests", site="topcoder") == "Error: site must be leetcode, codeforces or atcoder, not 'topcoder'."


def test_codeforces_calls_are_spaced_two_seconds_apart(monkeypatch):
    slept = []
    clock = [100.0]
    monkeypatch.undo()                                    # the autouse patches (incl. _cf_wait) off for this one
    monkeypatch.setattr(cp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cp.time, "sleep", lambda s: (slept.append(round(s, 2)), clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(cp, "_cf_last", 0.0)
    cp._cf_wait()
    clock[0] += 0.5
    cp._cf_wait()
    assert slept == [1.5]


# ---------------------------------------------------------------- registration
def test_the_tool_is_offered_in_both_spaces_with_a_small_schema():
    import json

    from app.agent.tool_schema import convert_tool_to_openai_schema
    from app.agent.tools.registry import tools_for_space

    for space in ("FREEFORM", "WORKSPACE"):
        tool = next(t for t in tools_for_space(space) if t.__name__ == "coding")
        schema = convert_tool_to_openai_schema(tool)
        assert set(schema["function"]["parameters"]["properties"]) == {"action", "site", "user", "query"}
        assert len(json.dumps(schema)) < 1000


def test_reads_run_unasked_and_schedule_is_a_write_for_taint_and_for_claims():
    from app.agent import taint
    from app.agent.orchestrator import changed_tools
    from app.agent.permissions import RiskTier, evaluate_tool_permission

    assert taint.SOURCES["coding"] and taint.is_sink("coding", {"action": "schedule"}) and not taint.is_sink("coding", {"action": "contests"})
    assert changed_tools([{"tool": "coding", "args": {"action": "contests"}, "status": "success"}]) == set()
    assert changed_tools([{"tool": "coding", "args": {"action": "pick", "query": "dp"}, "status": "success"}]) == set()
    assert changed_tools([{"tool": "coding", "args": {"action": "schedule", "query": "cf:1"}, "status": "success"}]) == {"coding"}
    decision = evaluate_tool_permission("coding", {"action": "contests"})
    assert decision.risk_tier == RiskTier.LOW_RISK and decision.allowed


# ---------------- found in review ----------------
@pytest.mark.parametrize("query,expected", [
    ("dp 1400", ("dp", "1400")), ("1400", ("", "1400")), ("graph, 1900", ("graph", "1900")), ("dp medium 1400", ("dp 1400", "medium"))])
def test_a_lone_rating_in_a_pick_query_is_a_level(query, expected):
    assert cp._split_query(query) == expected


def test_a_single_rating_picks_a_band_around_it(sites):
    sites.json[CF + "problemset.problems"] = cf_problems()
    out = coding(action="pick", site="codeforces", query="dp 1500")                       # 1400-1600: only the 1500 problem
    assert out.startswith("1 Codeforces problems match.") and "rating 1500" in out


def test_a_handle_without_a_site_asks_which_site_instead_of_claiming_there_is_none(sites):
    out = coding(action="profile", user="tourist")
    assert out == "Error: which site is 'tourist' on? Say leetcode, codeforces or atcoder."
    assert sites.calls == []                                                               # nothing was sent to any site


@pytest.mark.parametrize("query,days", [("14", 14), ("30 days", 30), (" 3 d ", 3), ("", 7), ("Div 2", 7), ("ABC 483", 7), ("weekend", 7), ("1000", 7)])
def test_only_a_bare_day_count_sets_the_window(query, days):
    assert cp._days(query) == days


def test_the_three_contest_sources_are_read_at_the_same_time(monkeypatch):
    def slow(name):
        def fn():
            time.sleep(0.3)
            return []
        return fn
    monkeypatch.setattr(cp, "_CONTEST_SOURCES", {s: slow(s) for s in cp.SITES})
    started = time.monotonic()
    found, failed = cp.upcoming_contests("", 7)
    assert time.monotonic() - started < 0.7 and found == [] and failed == []             # three in a row would be ~0.9 s


def test_a_site_that_hangs_or_breaks_is_reported_and_the_others_still_answer(sites):
    all_sites(sites)
    sites.graphql[0] = ("upcomingContests", RuntimeError("boom"))
    out = coding(action="contests")
    assert "Educational Codeforces Round 196" in out and "AtCoder Beginner Contest 483" in out
    assert "Couldn't read: LeetCode: its contest list could not be read." in out


def test_a_codeforces_message_with_a_url_is_not_a_file_claim_after_scheduling(sites, calendar_env):
    from app.agent.orchestrator import unbacked_claims

    used = [{"tool": "coding", "args": {"action": "schedule", "query": "cf:2283"}, "status": "success"}]
    for reply in ("Added Educational Codeforces Round 196 (https://codeforces.com/contests/2283) to your calendar with a reminder.",
                  "Done! Added the round on codeforces.com to your calendar and set a reminder for 14:20."):
        assert unbacked_claims(reply, used) == []
    assert unbacked_claims("I saved notes.md for you.", used) == ["the file"]              # a real file claim is still caught
