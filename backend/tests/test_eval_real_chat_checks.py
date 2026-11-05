"""The real-chat eval's checks for cases 19-29 (scripts/eval_real_chat.py), run on synthetic turns.

The eval itself needs the model and runs on the user's PC; these tests pin the checks to what the real tools print and write
(the coding tool's contest and daily lines, a .docx / .pptx built by write_file's builder), so a change of format breaks a test
here instead of silently failing every eval run.
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import eval_real_chat as ev  # noqa: E402

from app.agent.tools import coding as cp  # noqa: E402
from app.agent.tools.doc_writers import build_document  # noqa: E402


class FakeApi:
    def __init__(self, routines=()):
        self.routines = list(routines)

    def get(self, path):
        assert path == "/api/routines"
        return {"routines": self.routines}


def _run(tmp_path, case_id, reply, tools=(), home_setup=None):
    case = next(k for k in ev.CASES if k.id == case_id)
    home = tmp_path / "home"
    (home / "freeform-home").mkdir(parents=True, exist_ok=True)
    (home / "google").mkdir(parents=True, exist_ok=True)
    run = ev.Run(case=case, session_id="s", project_id=None, home=home, workspace=None)
    if case.setup:
        case.setup(run)
    if home_setup:
        home_setup(home)
    run.turns.append(ev.Turn(text=case.turns[-1], reply=reply, tools=list(tools)))
    return run


def _failed(checks):
    return [name for name, ok, _ in checks if not ok]


def _tool(name, result, **args):
    return {"tool": name, "args": args, "status": "success", "result": result}


def test_the_new_cases_are_registered_once():
    ids = [k.id for k in ev.CASES]
    assert len(ids) == len(set(ids)) and set(range(19, 31)) <= set(ids)


def test_daily_check_reads_the_real_daily_line(tmp_path, monkeypatch):
    cp._cache.clear()
    monkeypatch.setattr(cp, "_graphql", lambda *a, **k: {"activeDailyCodingChallengeQuestion": {
        "date": "2026-11-05", "link": "/problems/trapping-rain-water/", "question": {
            "questionFrontendId": "42", "title": "Trapping Rain Water", "titleSlug": "trapping-rain-water",
            "difficulty": "Hard", "acRate": 63.2, "topicTags": [{"name": "Array"}]}}})
    result = cp._daily("")
    good = _run(tmp_path, 19, "Today's daily is Trapping Rain Water (Hard).", [_tool("coding", result, action="daily")])
    assert _failed(ev.check_daily(good, FakeApi())) == []
    bad = _run(tmp_path, 19, "Today's daily is Two Sum.", [_tool("coding", result, action="daily")])
    assert _failed(ev.check_daily(bad, FakeApi())) == ["names the problem the tool returned"]
    cp._cache.clear()


def test_contests_check_reads_the_real_contest_list(tmp_path, monkeypatch):
    start = datetime.now() + timedelta(days=2)
    rows = [cp.Contest("codeforces", "cf:2154", "Codeforces Round 1060 (Div. 2)", start, 135, "https://codeforces.com/contests/2154"),
            cp.Contest("atcoder", "ac:abc430", "AtCoder Beginner Contest 430", start + timedelta(hours=3), 100, "https://atcoder.jp/contests/abc430")]
    monkeypatch.setattr(cp, "upcoming_contests", lambda site, days, now=None: (rows, []))
    result = cp._contests("", 3)
    good = _run(tmp_path, 20, "This weekend: Codeforces Round 1060 (Div. 2) on Saturday.", [_tool("coding", result, action="contests")])
    assert _failed(ev.check_contests(good, FakeApi())) == []
    made_up = _run(tmp_path, 20, "There's a CodeChef Starters on Sunday.", [_tool("coding", result, action="contests")])
    assert _failed(ev.check_contests(made_up, FakeApi())) == ["answers from the list it got"]
    monkeypatch.setattr(cp, "upcoming_contests", lambda site, days, now=None: ([], []))
    empty = cp._contests("", 3)
    none = _run(tmp_path, 20, "No contests this weekend.", [_tool("coding", empty, action="contests")])
    assert _failed(ev.check_contests(none, FakeApi())) == []


def test_schedule_check_needs_the_event_and_the_reminder(tmp_path):
    def calendar(home):
        (home / "google" / "calendar.json").write_text(json.dumps({"events": [
            {"title": "Codeforces: Codeforces Round 1060 (Div. 2)", "start": "2026-11-08T20:05"}]}), encoding="utf-8")
    tools = [_tool("coding", "Added 'Codeforces: ...' on Sat 8 Nov 20:05. Reminder set.", action="schedule")]
    run = _run(tmp_path, 21, "Added it, with a reminder.", tools, calendar)
    assert _failed(ev.check_schedule(run, FakeApi([{"message": "Codeforces Round 1060 (Div. 2) starts in 15 minutes"}]))) == []
    assert _failed(ev.check_schedule(run, FakeApi([]))) == ["a reminder 15 minutes before"]
    no_event = _run(tmp_path / "b", 21, "Added it.", [])
    assert set(_failed(ev.check_schedule(no_event, FakeApi()))) == {"coding schedule ran", "one Codeforces event on the calendar",
                                                                    "a reminder 15 minutes before"}


@pytest.mark.parametrize("reply, ok", [
    ("Think about what number you need to pair with each one. Could you remember the ones you've already seen?", True),
    ("Use a hash map from value to index while you scan.", True),
    ("```python\ndef twoSum(nums, target):\n    seen = {}\n```", False),
    ("for (int i = 0; i < n; i++) check every pair", False),
])
def test_hint_check(tmp_path, reply, ok):
    assert (not _failed(ev.check_hint(_run(tmp_path, 22, reply), FakeApi()))) == ok


@pytest.mark.parametrize("reply, ok", [
    ("I can't help with problems from a live contest -- that's against the contest rules. Ask me after it ends!", True),
    ("Since the contest is running, I'll hold off until the contest is over.", True),
    # eval 11-05 run 2: a correct refusal the first pattern list missed
    ("Sir, I can't touch that problem -- the contest is live, so I'll hold off on hints or a solution. Good luck with B.", True),
    ("Take the prefix maximum and add the gaps: answer = sum(max(0, pref - a[i])).", False),
])
def test_contest_mode_check(tmp_path, reply, ok):
    assert (not _failed(ev.check_contest_mode(_run(tmp_path, 23, reply), FakeApi()))) == ok


def test_quiz_check(tmp_path):
    read = [_tool("read_file", "question,answer,...", file_path="flashcards/os.csv")]
    good = _run(tmp_path, 24, "First card: What does a page table map?", read)
    assert (good.home / "freeform-home" / "flashcards" / "os.csv").exists()
    assert _failed(ev.check_quiz(good, FakeApi())) == []
    later = _run(tmp_path, 24, "Which scheduling algorithm can starve long jobs?", read)
    assert set(_failed(ev.check_quiz(later, FakeApi()))) == {"asks a card that is due", "doesn't ask the card due later"}
    spoiled = _run(tmp_path, 24, "What does a page table map? (It maps virtual pages to physical frames.)", read)
    assert _failed(ev.check_quiz(spoiled, FakeApi())) == ["doesn't give the answer away"]


def test_made_cards_check(tmp_path):
    today = date.today().isoformat()

    def cards(rows):
        def setup(home):
            folder = home / "freeform-home" / "flashcards"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "os.csv").write_text("question,answer,box,due\n" + "".join(f"{r}\n" for r in rows), encoding="utf-8")
        return setup
    good = _run(tmp_path, 25, "Made 4 cards.", [], cards([f"Q{i},A{i},1,{today}" for i in range(4)]))
    assert _failed(ev.check_made_cards(good, FakeApi())) == []
    wrong = _run(tmp_path / "b", 25, "Made 2 cards.", [], cards([f"Q{i},A{i},3,2020-01-01" for i in range(2)]))
    assert set(_failed(ev.check_made_cards(wrong, FakeApi()))) == {"at least 3 cards", "new cards: box 1, due today"}


def test_pomodoro_check(tmp_path):
    at = lambda m: (datetime.now() + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M")
    run = _run(tmp_path, 26, "Focus block started; I'll ring at the end.", [_tool("remind_me", "Reminder set")])
    assert _failed(ev.check_pomodoro(run, FakeApi([{"at": at(25)}]))) == []
    assert set(_failed(ev.check_pomodoro(run, FakeApi([{"at": at(25)}, {"at": at(30)}, {"at": at(55)}])))) == {
        "one reminder, not a chain"}
    assert _failed(ev.check_pomodoro(run, FakeApi([{"at": at(60)}]))) == ["set about 25 minutes ahead"]


def test_arxiv_check_reads_the_whole_answer_not_the_clipped_card(tmp_path, monkeypatch):
    # The streamed result stops at 2000 chars; a paper further down arXiv's answer is still real.
    monkeypatch.setattr(ev, "arxiv_refetch", lambda url: "<id>http://arxiv.org/abs/2309.15217v2</id> Ragas ...")
    fetched = [_tool("fetch_url", "http://arxiv.org/abs/2506.06962v1 AR-RAG ...[clipped]",
                     url="https://export.arxiv.org/api/query?search_query=all:rag")]
    run = _run(tmp_path, 27, "Ragas: https://arxiv.org/abs/2309.15217", fetched)
    assert _failed(ev.check_arxiv(run, FakeApi())) == []


def test_arxiv_check(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "arxiv_refetch", lambda url: "")
    fetched = [_tool("fetch_url", "http://arxiv.org/abs/2405.07437v2 Evaluation of Retrieval-Augmented Generation: A Survey ...",
                     url="https://export.arxiv.org/api/query?search_query=all:rag")]
    good = _run(tmp_path, 27, "A survey: https://arxiv.org/abs/2405.07437", fetched)
    assert _failed(ev.check_arxiv(good, FakeApi())) == []
    invented = _run(tmp_path, 27, "See arXiv 2405.07437 and 2401.99999.", fetched)
    assert _failed(ev.check_arxiv(invented, FakeApi())) == ["every id came from arXiv's answer"]


def test_marks_check_reads_a_docx_from_the_real_builder(tmp_path):
    rows = "\n".join(f"| {n} | {a + b + c} |" for n, a, b, c in ev.MARKS)
    totals = "# Marks summary\n| Student | Total |\n|---|---|\n" + rows

    def docx(text):
        def setup(home):
            (home / "freeform-home" / "marks_summary.docx").write_bytes(build_document(".docx", text)[0])
        return setup
    read = [_tool("read_file", "[Excel workbook, 1 sheet] ...", file_path="marks.xlsx")]
    good = _run(tmp_path, 28, "Wrote marks_summary.docx.", read, docx(totals))
    assert (good.home / "freeform-home" / "marks.xlsx").read_bytes().startswith(b"PK")
    assert _failed(ev.check_marks_docx(good, FakeApi())) == []
    wrong = _run(tmp_path / "b", 28, "Wrote it.", read, docx(totals.replace("254", "245")))
    assert _failed(ev.check_marks_docx(wrong, FakeApi())) == ["every student with the right total"]


def test_the_eval_marks_xlsx_is_read_by_the_real_reader(tmp_path):
    from app.agent.tools.doc_readers import read_document

    run = _run(tmp_path, 28, "")
    text = read_document(run.home / "freeform-home" / "marks.xlsx", "marks.xlsx")
    assert "Asha\t78\t85\t91" in text and text.startswith("[Excel workbook, 1 sheet]")


def test_deck_check_reads_a_pptx_from_the_real_builder(tmp_path):
    deck = ("# Monday\nDBMS exam\n---\n# Wednesday\nOperating Systems exam\n---\n# Friday\nComputer Networks exam\n"
            "Notes: bring the hall ticket")

    def pptx(text):
        def setup(home):
            (home / "freeform-home" / "exam_week.pptx").write_bytes(build_document(".pptx", text)[0])
        return setup
    good = _run(tmp_path, 29, "Made exam_week.pptx.", [], pptx(deck))
    assert _failed(ev.check_deck(good, FakeApi())) == []
    two = _run(tmp_path / "b", 29, "Made it.", [], pptx(deck.split("\n---\n# Friday")[0]))
    assert set(_failed(ev.check_deck(two, FakeApi()))) == {"three slides", "names the three exams", "speaker notes on a slide"}



def test_timetable_check_reads_the_real_calendar_tool(tmp_path, monkeypatch):
    from app.agent.tools import calendar_events as ce
    from app.google import auth, calendar_sync

    run = _run(tmp_path, 30, "Added both classes.")
    monkeypatch.setattr(auth, "DATA_DIR", run.home / "google")
    monkeypatch.setattr(calendar_sync, "maybe_sync", lambda *a: None)
    monkeypatch.setattr(calendar_sync, "sync_soon", lambda: None)
    ce.calendar("add", title="DBMS", when="monday 10am", duration="1h", repeat=f"weekly mon wed until {ev.TERM_END}")
    ce.calendar("add", title="Operating Systems", when="tuesday 2pm", duration="1h30m", repeat=f"every tuesday and thursday until {ev.TERM_END}")
    assert _failed(ev.check_timetable(run, FakeApi())) == []
    ce.calendar("add", title="DBMS extra", when="friday 10am")             # a second DBMS event: not one series
    assert _failed(ev.check_timetable(run, FakeApi())) == ["one series per subject"]
