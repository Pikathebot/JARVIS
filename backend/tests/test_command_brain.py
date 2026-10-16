"""Voice-mode command brain: the code-owned half (candidates, shortlist, plan building).

Laya itself is faked: these tests pin what code extracts and how answers become a Plan.
"""
from app.voice import command_brain as brain

APPS = ["Google Chrome", "Discord", "Visual Studio Code", "Settings", "WSL Settings", "OBS Studio (64bit)",
        "Steam", "Steam Support Center", "Notepad"]


def test_text_candidates_offer_the_clean_payload():
    cases = {
        "search youtube for lofi hip hop": "lofi hip hop",
        "youtube search lofi hip hop": "lofi hip hop",
        "look up cat videos on amazon please": "cat videos",
        "find me gaming chairs on amazon for me": "gaming chairs",
        "type see you at five and hit enter": "see you at five",
        "type out see you at five real quick": "see you at five",
        "write down ok": "ok",
    }
    for utterance, payload in cases.items():
        assert payload in brain.text_candidates(utterance).values(), utterance


def test_trailing_place_is_an_extra_candidate_not_a_replacement():
    cands = list(brain.text_candidates("type meet me in the park").values())
    assert "meet me in the park" in cands and "meet me" in cands


def test_whole_utterance_is_always_a_candidate():
    u = "search for a b c on d e for f of g about h"
    assert u in brain.text_candidates(u).values()


def test_reminder_candidates():
    what, when = brain.reminder_candidates("remind me to call mom at 6pm")
    assert "call mom" in what.values() and "at 6pm" in when.values()
    _, when = brain.reminder_candidates("set a timer for 10 minutes")
    assert "for 10 minutes" in when.values()


def test_app_shortlist_matches_words_and_misspellings():
    assert brain.app_shortlist("open chrome", APPS)[0] == "Google Chrome"
    assert brain.app_shortlist("fire up obs", APPS) == ["OBS Studio (64bit)"]
    assert brain.app_shortlist("open dicsord", APPS) == ["Discord"]
    assert brain.app_shortlist("open the settings", APPS)[0] == "Settings"
    assert brain.app_shortlist("open", APPS) == []
    assert len(brain.app_shortlist("steam", ["Steam %d" % i for i in range(40)])) == brain.APP_SHORTLIST


def test_a_different_app_is_not_a_near_spelling():
    # 2026-10-02: "open photoshop" shortlisted Photos (0.80) and Laya opened it at 0.97.
    assert brain.app_shortlist("open photoshop", APPS + ["Photos"]) == []


def test_looks_compound_backstop():
    assert brain.looks_compound("open notepad and type hello there")
    assert brain.looks_compound("open chrome then go to youtube")
    assert not brain.looks_compound("type salt and pepper")
    assert not brain.looks_compound("search for rock and roll")


def test_domain_guess_and_wake_word():
    assert brain.domain_guess("go to stackoverflow dot com") == "stackoverflow.com"
    assert brain.domain_guess("visit nvidia.com") == "nvidia.com"
    assert brain.domain_guess("go to reddit") == "reddit.com"
    # Glued words were wrong more often than right: these search instead.
    for said in ["open the steam store page", "go to the rust docs", "go to hacker news", "go to the docs"]:
        assert brain.domain_guess(said) is None, said
    assert brain.strip_wake_word("Hey Jarvis, open chrome") == "open chrome"


def test_split_compound_keeps_enter_with_the_typing():
    assert brain.split_compound("open notepad and type buy milk") == ["open notepad", "type buy milk"]
    assert brain.split_compound("type hello and hit enter") == ["type hello and hit enter"]


def test_no_question_exceeds_laya_option_limit():
    ctx = brain.build_context("open chrome", APPS)
    qs = dict(brain.stage1_questions())
    for action in brain.ACTIONS:
        qs.update({f"{action}.{k}": v for k, v in brain.stage2_questions(action, ctx).items()})
    for qid, q in qs.items():
        assert len(q["criteria"]) <= 30, qid


class FakeLaya:
    """Answers each question with a scripted choice; records what it was asked."""

    def __init__(self, picks: dict):
        self.picks, self.calls = picks, []

    def __call__(self, state, questions):
        self.calls.append((state, questions))
        out = {}
        for qid, q in questions.items():
            pick = self.picks.get(qid)
            if callable(pick):
                pick = pick(q["criteria"])
            pick = pick if pick is not None else next(iter(q["criteria"]))
            out[qid] = {"type": "choice", "choice": pick, "answer_confidence": 0.9,
                        "probabilities": {k: (0.9 if k == pick else 0.0) for k in q["criteria"]}}
        return out


def _key_of(text):
    return lambda crit: next(k for k, v in crit.items() if v == text)


def test_evaluate_type_text_selects_a_span():
    fake = FakeLaya({"action": "type_text", "compound": "B", "text": _key_of("git status"), "submit": "A"})
    plan = brain.evaluate("jarvis, type git status and press enter", fake, APPS)
    assert plan.action == "type_text"
    assert plan.args == {"text": "git status", "submit": True}
    assert len(fake.calls) == 2 and "candidates" in fake.calls[1][0]
    assert plan.utterance == "type git status and press enter"


def test_evaluate_open_app_only_shows_the_shortlist():
    fake = FakeLaya({"action": "open_app", "compound": "B", "app": "Google Chrome"})
    plan = brain.evaluate("open chrome", fake, APPS)
    assert plan.args == {"app": "Google Chrome"}
    assert "Discord" not in fake.calls[1][1]["app"]["criteria"]


def test_evaluate_unknown_site_falls_back_to_domain_then_search():
    fake = FakeLaya({"action": "open_website", "compound": "B", "site": "other"})
    assert brain.evaluate("go to stackoverflow dot com", fake, APPS).args["url"] == "https://stackoverflow.com"
    fake = FakeLaya({"action": "open_website", "compound": "B", "site": "other", "engine": "google"})
    plan = brain.evaluate("go to the rust standard library docs", fake, APPS)
    assert plan.action == "web_search"
    # Not a request to go somewhere, or it refers back: MiniCPM sees the conversation.
    for said in ["open that thing we talked about", "Can you click into the rising ecstasy server?"]:
        assert brain.evaluate(said, fake, APPS).action == "question", said


def test_search_needs_a_search_cue_and_a_real_query():
    # The device misses from 2026-10-04: each became a confident Google search.
    for said, query in [("Search the link for it please.", "it"),
                        ("Can you download it?", "Can you download it"),
                        ("I meant the Blackmeat Wukong mod link, not the Hit movie.", "I meant the Blackmeat Wukong mod link"),
                        ("Can you click into the rising ecstasy server?", "Can you click into the rising ecstasy server")]:
        assert not brain.search_is_grounded(said, query), said
    for said, query in [("search youtube for lofi hip hop", "lofi hip hop"),
                        ("google best ramen near me", "best ramen near me"),
                        ("look up rtx 5070 laptop price on amazon", "rtx 5070 laptop price"),
                        ("find llama.cpp on github", "llama.cpp"),
                        ("show me pictures of red pandas", "red pandas"),
                        ("Search about the multiplayer mod in Blackmeath, Wukong.", "the multiplayer mod in Blackmeath, Wukong")]:
        assert brain.search_is_grounded(said, query), said


def test_evaluate_ungrounded_search_goes_to_minicpm():
    fake = FakeLaya({"action": "web_search", "compound": "B", "engine": "google",
                     "text": lambda crit: next((k for k, v in crit.items() if v == "it"), next(iter(crit)))})
    plan = brain.evaluate("search the link for it please", fake, APPS)
    assert plan.action == "question"
    fake = FakeLaya({"action": "web_search", "compound": "B", "engine": "google"})
    assert brain.evaluate("can you download it?", fake, APPS).action == "question"


def test_evaluate_conversation_needs_one_pass_and_confidence_is_the_minimum():
    fake = FakeLaya({"action": "none", "compound": "B"})
    plan = brain.evaluate("thanks jarvis", fake, APPS)
    assert plan.action == "none" and len(fake.calls) == 1
    fake = FakeLaya({"action": "volume", "compound": "B", "volume_op": "set_level"})
    plan = brain.evaluate("set the volume to loud", fake, APPS)  # no number said
    assert plan.confidence == 0.0
