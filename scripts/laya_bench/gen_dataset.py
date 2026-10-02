"""Generate Laya fine-tuning data for the voice-mode command brain (PLAN 4.9).

Every row is one (state, question, gold answer) built with the brain's *own* question text,
state layout and candidate extraction (``backend/app/voice/command_brain.py``), so the model is
trained on exactly what it is asked at runtime. Utterances come from templates x fillers; any
utterance that appears in the held-out ``commands.jsonl`` is dropped.

    tools\\laya-venv\\Scripts\\python.exe scripts\\laya_bench\\gen_dataset.py   # -> data/train.jsonl
"""
from __future__ import annotations

import importlib.util
import json
import random
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
_spec = importlib.util.spec_from_file_location("command_brain", ROOT / "backend/app/voice/command_brain.py")
brain = importlib.util.module_from_spec(_spec)
sys.modules["command_brain"] = brain
_spec.loader.exec_module(brain)

R = random.Random(20261002)
APPS = json.loads((HERE / "apps.json").read_text(encoding="utf-8"))

PLEASE = ["", "", "", "please ", "can you ", "could you ", "jarvis ", "jarvis, ", "hey jarvis ", "ok jarvis "]
TAIL = ["", "", "", "", " please", " for me", " now", " real quick"]


def lead(s: str) -> str:
    return R.choice(PLEASE) + s + R.choice(TAIL)


# ----------------------------------------------------------------------------- fillers

# (spoken name, Start-menu name)
SPOKEN_APPS = [
    ("chrome", "Google Chrome"), ("google chrome", "Google Chrome"), ("vs code", "Visual Studio Code"),
    ("visual studio code", "Visual Studio Code"), ("vscode", "Visual Studio Code"),
    ("discord", "Discord"), ("steam", "Steam"), ("blender", "Blender"), ("file explorer", "File Explorer"),
    ("explorer", "File Explorer"), ("obs", "OBS Studio (64bit)"), ("obs studio", "OBS Studio (64bit)"),
    ("settings", "Settings"), ("windows settings", "Settings"), ("task manager", "Task Manager"),
    ("word", "Word"), ("microsoft word", "Word"), ("excel", "Excel"), ("powerpoint", "PowerPoint"),
    ("outlook", "Outlook (classic)"), ("onenote", "OneNote"), ("unity hub", "Unity Hub"), ("unity", "Unity"),
    ("spotify", "Spotify"), ("notepad", "Notepad"), ("calculator", "Calculator"), ("the calculator", "Calculator"),
    ("paint", "Paint"), ("vlc", "VLC media player"), ("vlc player", "VLC media player"), ("brave", "Brave"),
    ("edge", "Microsoft Edge"), ("microsoft edge", "Microsoft Edge"), ("epic games", "Epic Games Launcher"),
    ("the epic launcher", "Epic Games Launcher"), ("valorant", "VALORANT"), ("android studio", "Android Studio"),
    ("git bash", "Git Bash"), ("powershell", "Windows PowerShell"), ("command prompt", "Command Prompt"),
    ("cmd", "Command Prompt"), ("zoom", "Zoom Workplace"), ("qbittorrent", "qBittorrent"),
    ("the torrent client", "qBittorrent"), ("wallpaper engine", "Wallpaper Engine"), ("nvidia app", "NVIDIA App"),
    ("the nvidia app", "NVIDIA App"), ("winrar", "WinRAR"), ("sticky notes", "Sticky Notes (new)"),
    ("onedrive", "OneDrive"), ("heidi sql", "HeidiSQL"), ("pgadmin", "pgAdmin 4"), ("rainmeter", "Rainmeter"),
    ("hwinfo", "HWiNFO® 64"), ("registry editor", "Registry Editor"), ("event viewer", "Event Viewer"),
    ("the camera", "Camera"), ("camera", "Camera"), ("photos", "Photos"), ("the clock app", "Clock"),
    ("microsoft store", "Microsoft Store"), ("the store", "Microsoft Store"), ("snipping tool", "Snipping Tool"),
    ("windows terminal", "Windows Terminal"), ("terminal", "Windows Terminal"), ("elden ring", "ELDEN RING NIGHTREIGN"),
    ("wuthering waves", "Wuthering Waves"), ("genshin", "Genshin Impact Beta"), ("riot client", "Riot Client"),
    ("ubisoft connect", "Ubisoft Connect"), ("parsec", "Parsec"), ("comfy", "Comfy Desktop"),
    ("stability matrix", "Stability Matrix"), ("unsloth studio", "Unsloth Studio"), ("qwen", "Qwen"),
    ("visual studio", "Visual Studio 2022"), ("eclipse", "Eclipse IDE for Java Developers - 2025-03"),
    ("python", "Python 3.14"), ("firefox", "Firefox Private Browsing"), ("tor browser", "Tor Browser"),
]
MISSING_APPS = ["photoshop", "slack", "telegram", "figma", "notion", "premiere", "audacity", "gimp",
                "minecraft", "teams", "whatsapp desktop", "obsidian", "davinci resolve", "postman"]

OPEN_APP_T = ["open {a}", "launch {a}", "start {a}", "switch to {a}", "bring up {a}", "fire up {a}",
              "run {a}", "open up {a}", "go to {a}", "pull up {a}", "i need {a}", "get {a} open",
              "open the {a} app", "start up {a}", "show me {a}", "jump to {a}", "focus {a}", "can i get {a}"]

SITE_SPOKEN = {
    "youtube": ["youtube", "you tube", "yt"], "gmail": ["gmail", "my gmail", "my email inbox in gmail"],
    "google": ["google", "google dot com"], "github": ["github", "git hub"], "reddit": ["reddit"],
    "wikipedia": ["wikipedia"], "amazon": ["amazon"], "netflix": ["netflix"], "twitter": ["twitter", "x dot com"],
    "instagram": ["instagram", "insta"], "whatsapp": ["whatsapp web"], "chatgpt": ["chatgpt", "chat gpt"],
    "claude": ["claude", "claude dot ai"], "huggingface": ["hugging face", "huggingface"],
    "google drive": ["google drive", "my drive"], "google maps": ["google maps"],
}
OTHER_SITES = ["stackoverflow.com", "nvidia.com", "bbc.co.uk", "twitch.tv", "espn.com", "cnn.com", "imdb.com",
               "openai.com", "anthropic.com", "python.org", "pypi.org", "steamdb.info", "ebay.com", "spotify.com",
               "duolingo.com", "canva.com", "figma.com", "notion.so", "linkedin.com", "kaggle.com"]
OPEN_SITE_T = ["go to {s}", "open {s}", "take me to {s}", "pull up {s}", "visit {s}", "navigate to {s}",
               "load {s}", "open the {s} website", "go to the {s} site", "bring up {s} in the browser",
               "open {s} in chrome", "head to {s}", "show me {s}"]

QUERIES = ["lofi hip hop", "best ramen near me", "how to fix a leaking tap", "rtx 4060 benchmarks",
           "cheap flights to london", "python list comprehension", "elden ring boss guide", "cat videos",
           "the roman empire", "weather in chennai", "llama.cpp", "gaming chairs", "taylor swift new album",
           "how to tie a tie", "chocolate cake recipe", "used cars under ten lakhs", "blender donut tutorial",
           "pikachu wallpaper", "mechanical keyboards", "the french revolution", "unity shader graph",
           "noise cancelling headphones", "how to center a div", "quantum computing basics", "kokoro tts",
           "red pandas", "monster hunter wilds tips", "4k nature videos", "black holes", "wireless mouse"]
ENGINE_SPOKEN = {"google": ["google"], "youtube": ["youtube"], "amazon": ["amazon"], "wikipedia": ["wikipedia"],
                 "github": ["github"], "reddit": ["reddit"], "google maps": ["google maps", "maps"],
                 "google images": ["google images", "images"], "huggingface": ["hugging face"]}
SEARCH_T_ENGINE = ["search {e} for {q}", "search for {q} on {e}", "look up {q} on {e}", "find {q} on {e}",
                   "{e} search {q}", "search {q} on {e}", "look for {q} on {e}", "find me {q} on {e}"]
SEARCH_T_PLAIN = ["google {q}", "search for {q}", "look up {q}", "search {q}", "search the web for {q}",
                  "find {q} online", "do a search for {q}", "google search {q}"]
IMAGE_T = ["show me pictures of {q}", "find images of {q}", "show me photos of {q}"]
VIDEO_T = ["play {q} on youtube", "find videos of {q}", "show me videos of {q}"]

TYPE_TEXTS = ["hello world", "see you at five", "meeting moved to thursday", "on my way", "sounds good",
              "the build passed", "thanks for the update", "git status", "npm run dev", "buy milk and eggs",
              "lunch at one", "ok", "i'll check it tonight", "dear sir", "call me when you're free",
              "python main.py", "ls", "happy birthday", "good morning everyone", "let's ship it",
              "remember to water the plants", "test one two three", "cd backend", "yes that works for me"]
TYPE_T = ["type {t}", "type out {t}", "write {t}", "dictate {t}", "enter {t}", "input {t}", "put {t}",
          "type in {t}", "type the words {t}", "write down {t}", "type the text {t}", "insert {t}"]
TYPE_SUBMIT = [" and hit enter", " and press enter", " and send it", " then press enter", " and hit return",
               " and submit"]

COMPOSE_T = ["reply saying i'll be a bit late", "write a short thank you note to the team",
             "answer this email saying yes", "draft a polite message declining the invite",
             "write an apology for missing the meeting", "reply that i'm interested and ask for the price",
             "write a professional email asking for an extension", "compose a tweet about the new release",
             "write a birthday message for my friend", "reply to this telling them i agree",
             "write a short bio about me as a developer", "summarize what i just said into a message",
             "write a polite reminder about the unpaid invoice", "respond saying thanks but no thanks",
             "write a two line description of this project", "reply asking when they are free",
             "say sorry for the late reply and that i'll check tomorrow", "write a funny caption for this",
             "make a nice message to congratulate them on the promotion", "write a cover letter opening"]
COMPOSE_STYLE = ["", "", ", politely", ", keep it short", ", make it formal", ", in a friendly tone"]
COMPOSE_SEND = [" and send it", " then send it", " and hit enter"]

SHORTCUT_SPOKEN = {
    "enter": ["press enter", "hit enter", "hit return", "submit", "enter key", "press return"],
    "escape": ["press escape", "hit escape", "escape", "cancel that dialog"],
    "tab": ["press tab", "hit tab", "tab key"],
    "backspace": ["backspace", "press backspace", "delete the last letter"],
    "delete_word": ["delete the last word", "delete that word", "remove the previous word"],
    "copy": ["copy", "copy that", "copy this", "copy it", "copy the selection"],
    "paste": ["paste", "paste it", "paste that", "paste here"],
    "cut": ["cut", "cut that", "cut this"],
    "undo": ["undo", "undo that", "undo the last change", "take that back"],
    "redo": ["redo", "redo that"],
    "select_all": ["select all", "select everything", "highlight everything"],
    "save": ["save", "save it", "save the file", "save this"],
    "find": ["find in page", "open find", "search this page"],
    "new_tab": ["new tab", "open a new tab", "open another tab"],
    "close_tab": ["close the tab", "close this tab", "close tab"],
    "reopen_tab": ["reopen the closed tab", "bring back that tab", "reopen last tab"],
    "next_tab": ["next tab", "switch to the next tab", "go to the next tab"],
    "previous_tab": ["previous tab", "go to the previous tab", "last tab"],
    "back": ["go back", "back", "go back a page", "previous page"],
    "forward": ["go forward", "forward", "next page"],
    "reload": ["reload", "refresh", "refresh the page", "reload this page"],
    "address_bar": ["focus the address bar", "go to the url bar", "select the address bar"],
    "close_window": ["close the window", "close this window", "quit this app", "close the app", "exit this program"],
    "minimize": ["minimize", "minimize this window", "minimize it", "hide this window"],
    "maximize": ["maximize", "maximize the window", "full screen", "make it full screen"],
    "switch_app": ["alt tab", "switch to the last app", "switch apps", "go to the previous app"],
    "zoom_in": ["zoom in", "make it bigger", "make the text bigger"],
    "zoom_out": ["zoom out", "make it smaller", "make the text smaller"],
}

SCROLL = [
    ("scroll down", "down", "page"), ("scroll up", "up", "page"), ("scroll down a little", "down", "little"),
    ("scroll up a bit", "up", "little"), ("scroll down a bit", "down", "little"), ("scroll a lot", "down", "a_lot"),
    ("scroll way down", "down", "a_lot"), ("scroll way up", "up", "a_lot"), ("scroll down a lot", "down", "a_lot"),
    ("go down", "down", "page"), ("page down", "down", "page"), ("page up", "up", "page"),
    ("go to the top", "top", "page"), ("scroll to the top", "top", "page"), ("back to the top", "top", "page"),
    ("go to the bottom", "bottom", "page"), ("scroll to the bottom", "bottom", "page"),
    ("jump to the end of the page", "bottom", "page"), ("jump to the top of the page", "top", "page"),
    ("move down a little", "down", "little"), ("scroll up slightly", "up", "little"), ("keep scrolling", "down", "page"),
]

VOLUME = [
    ("turn it up", "up"), ("louder", "up"), ("volume up", "up"), ("turn the volume up", "up"),
    ("a bit louder", "up"), ("raise the volume", "up"), ("turn it down", "down"), ("quieter", "down"),
    ("volume down", "down"), ("lower the volume", "down"), ("turn the volume down a bit", "down"),
    ("way louder", "up_a_lot"), ("make it way louder", "up_a_lot"), ("crank it up", "up_a_lot"),
    ("much louder", "up_a_lot"), ("way quieter", "down_a_lot"), ("turn it way down", "down_a_lot"),
    ("much quieter", "down_a_lot"), ("mute", "mute"), ("mute the sound", "mute"), ("mute it", "mute"),
    ("silence", "mute"), ("mute the audio", "mute"), ("unmute", "unmute"), ("unmute the sound", "unmute"),
    ("sound back on", "unmute"), ("turn the sound back on", "unmute"), ("max volume", "max"),
    ("volume all the way up", "max"), ("full volume", "max"),
]
VOLUME_LEVEL_T = ["set the volume to {n}", "volume {n}", "volume to {n} percent", "put the volume at {n}",
                  "change the volume to {n}", "set volume {n} percent"]

MEDIA = [
    ("play", "play"), ("play the music", "play"), ("resume", "play"), ("resume the video", "play"),
    ("continue playing", "play"), ("unpause", "play"), ("play it again", "play"), ("pause", "pause"),
    ("pause the music", "pause"), ("pause the video", "pause"), ("pause it", "pause"), ("hold the music", "pause"),
    ("stop the music", "pause"), ("stop the song", "pause"), ("next song", "next"), ("skip", "next"),
    ("skip this song", "next"), ("next track", "next"), ("skip this", "next"), ("play the next one", "next"),
    ("previous song", "previous"), ("previous track", "previous"), ("go back a song", "previous"),
    ("play the last song again", "previous"), ("back one track", "previous"),
]

FOLDER_SPOKEN = {"downloads": ["downloads", "my downloads", "the downloads folder"],
                 "documents": ["documents", "my documents", "the documents folder"],
                 "desktop": ["the desktop folder", "my desktop folder"], "pictures": ["pictures", "my pictures", "my photos folder"],
                 "music": ["my music folder", "the music folder"], "videos": ["my videos", "the videos folder"],
                 "home": ["my home folder", "my user folder"], "this pc": ["this pc", "my computer"]}
FOLDER_T = ["open {f}", "show me {f}", "open up {f}", "go to {f}", "take me to {f}"]

SYSTEM = [
    ("lock the computer", "lock"), ("lock my pc", "lock"), ("lock the screen", "lock"), ("lock it", "lock"),
    ("show the desktop", "show_desktop"), ("show me the desktop", "show_desktop"),
    ("minimize everything", "show_desktop"), ("hide all windows", "show_desktop"),
    ("take a screenshot", "screenshot"), ("screenshot", "screenshot"), ("grab a screenshot", "screenshot"),
    ("capture the screen", "screenshot"), ("turn off the screen", "screen_off"), ("screen off", "screen_off"),
    ("turn the display off", "screen_off"), ("put the monitor to sleep", "screen_off"),
]

REMIND_WHAT = ["stretch", "call mom", "drink water", "check the oven", "take my medicine", "join the meeting",
               "submit the assignment", "feed the cat", "go to the gym", "reply to sam", "charge my phone",
               "push the code", "book the tickets", "water the plants"]
REMIND_WHEN = ["in 20 minutes", "in an hour", "in 5 minutes", "at 6pm", "at 7:30 pm", "tomorrow 9am",
               "tomorrow at 8 am", "in half an hour", "at 10 am", "tonight at 9pm", "in 2 hours", "at 3pm"]
REMIND_T = ["remind me to {w} {t}", "remind me {t} to {w}", "set a reminder to {w} {t}",
            "{t} remind me to {w}", "can you remind me to {w} {t}", "remind me about {w} {t}"]
TIMER_T = ["set a timer for {n} minutes", "timer for {n} minutes", "start a {n} minute timer",
           "set a timer for {n} seconds", "set an alarm for {n} minutes"]

QUESTIONS = ["what's the weather like today", "what's on my screen", "what am i looking at", "what song is this",
             "what's playing right now", "how hot is my gpu", "how much ram am i using", "what time is it",
             "what's the date today", "who won the champions league final", "what's the capital of australia",
             "how far is the moon", "what does this error mean", "read what's on the screen",
             "is my battery low", "what's the news today", "how many reminders do i have",
             "what's in my clipboard", "what's the price of bitcoin", "how do i say thanks in japanese",
             "explain what a transformer model is", "what's the score of the india match",
             "which app is using the most memory", "how's my cpu doing", "what did i ask you earlier",
             "can you tell me who directed inception", "what's twelve times eight"]

STOP = ["stop listening", "turn off voice mode", "voice mode off", "exit voice mode", "go to sleep",
        "that's all for now", "stop voice mode", "you can stop listening", "end voice mode",
        "goodbye jarvis", "that's it, go to sleep", "leave voice mode", "stop, i'm done"]

NONE = ["thanks", "thank you jarvis", "haha that's funny", "nice", "cool", "ok cool", "never mind",
        "yeah i'll call you back later", "how are you doing", "good job", "i'm tired today", "hmm",
        "wait what", "that's interesting", "let me think", "oh no", "you're awesome", "brb",
        "mom i'm in a meeting", "what were we talking about again", "tell me a joke",
        "i love this song", "ugh this game is hard", "good morning", "good night jarvis",
        "sorry not you", "talking to my friend", "anyway", "so yeah", "can't believe it"]


# ----------------------------------------------------------------------------- build


def norm(s: str) -> str:
    return " ".join(s.lower().replace("'", "").split()).strip(" .!?,")


HELD_OUT = {norm(json.loads(line)["u"]) for line in (HERE / "commands.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()}
rows: list[dict] = []
dropped = Counter()


def find_key(cands: dict, gold: str) -> str | None:
    for k, v in cands.items():
        if norm(v) == norm(gold):
            return k
    return None


def emit(u: str, action: str, gold2: dict | None = None, compound: bool = False) -> None:
    """One utterance -> its stage-1 rows (+ stage-2 rows for a single command)."""
    u = " ".join(u.split())
    if norm(brain.strip_wake_word(u)) in HELD_OUT:
        dropped["held_out"] += 1
        return
    utt = brain.strip_wake_word(u)
    ctx = brain.build_context(utt, APPS)
    st1 = brain._state(utt, ctx, None, "")
    q1 = brain.stage1_questions()
    rows.append({"u": utt, "state": st1, "qid": "action", "q": q1["action"], "gold": action})
    rows.append({"u": utt, "state": st1, "qid": "compound", "q": q1["compound"], "gold": "A" if compound else "B"})
    if compound or not gold2:
        return
    q2 = brain.stage2_questions(action, ctx)
    st2 = brain._state(utt, ctx, action, "")
    for qid, g in gold2.items():
        if qid not in q2:
            dropped[f"no_q:{action}.{qid}"] += 1
            continue
        crit = q2[qid]["criteria"]
        if qid in ("text", "remind_what", "remind_when"):
            g = find_key(crit, g)
            if g is None:
                dropped[f"no_cand:{action}.{qid}"] += 1
                continue
        if g not in crit:
            dropped[f"not_option:{action}.{qid}"] += 1
            continue
        rows.append({"u": utt, "state": st2, "qid": qid, "q": q2[qid], "gold": g})


def yn(b: bool) -> str:
    return "A" if b else "B"


def gen() -> None:
    for _ in range(3):
        for spoken, canon in SPOKEN_APPS:
            emit(lead(R.choice(OPEN_APP_T).format(a=spoken)), "open_app", {"app": canon})
    for _ in range(25):
        emit(lead(R.choice(OPEN_APP_T).format(a=R.choice(MISSING_APPS))), "open_app", {"app": "none"})

    for _ in range(12):
        for key, spoken in SITE_SPOKEN.items():
            emit(lead(R.choice(OPEN_SITE_T).format(s=R.choice(spoken))), "open_website", {"site": key})
    for _ in range(8):
        for dom in OTHER_SITES:
            spoken = dom if R.random() < 0.5 else dom.replace(".", " dot ")
            emit(lead(R.choice(OPEN_SITE_T).format(s=spoken)), "open_website", {"site": "other"})

    for _ in range(14):
        for key, spoken in ENGINE_SPOKEN.items():
            q = R.choice(QUERIES)
            emit(lead(R.choice(SEARCH_T_ENGINE).format(e=R.choice(spoken), q=q)), "web_search",
                 {"engine": key, "text": q})
    for _ in range(90):
        q = R.choice(QUERIES)
        emit(lead(R.choice(SEARCH_T_PLAIN).format(q=q)), "web_search", {"engine": "google", "text": q})
    for _ in range(20):
        q = R.choice(QUERIES)
        emit(lead(R.choice(IMAGE_T).format(q=q)), "web_search", {"engine": "google images", "text": q})
        q = R.choice(QUERIES)
        emit(lead(R.choice(VIDEO_T).format(q=q)), "web_search", {"engine": "youtube", "text": q})

    for _ in range(14):
        for t in TYPE_TEXTS:
            submit = R.random() < 0.3
            u = R.choice(TYPE_T).format(t=t) + (R.choice(TYPE_SUBMIT) if submit else "")
            emit(R.choice(PLEASE) + u, "type_text", {"text": t, "submit": yn(submit)})

    for _ in range(14):
        for c in COMPOSE_T:
            send = R.random() < 0.3
            emit(R.choice(PLEASE) + c + R.choice(COMPOSE_STYLE) + (R.choice(COMPOSE_SEND) if send else ""),
                 "compose", {"submit": yn(send)})

    for _ in range(9):
        for key, spoken in SHORTCUT_SPOKEN.items():
            emit(lead(R.choice(spoken)), "shortcut", {"shortcut": key})

    for _ in range(14):
        for u, d, a in SCROLL:
            emit(lead(u), "scroll", {"scroll_dir": d, "scroll_amount": a})

    for _ in range(10):
        for u, op in VOLUME:
            emit(lead(u), "volume", {"volume_op": op})
    for _ in range(60):
        n = R.choice([5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 100])
        emit(lead(R.choice(VOLUME_LEVEL_T).format(n=n)), "volume", {"volume_op": "set_level", "level": str(n)})

    for _ in range(12):
        for u, op in MEDIA:
            emit(lead(u), "media", {"media_op": op})

    for _ in range(10):
        for key, spoken in FOLDER_SPOKEN.items():
            emit(lead(R.choice(FOLDER_T).format(f=R.choice(spoken))), "open_folder", {"folder": key})

    for _ in range(14):
        for u, op in SYSTEM:
            emit(lead(u), "system", {"system_op": op})

    for _ in range(220):
        w, t = R.choice(REMIND_WHAT), R.choice(REMIND_WHEN)
        emit(R.choice(PLEASE) + R.choice(REMIND_T).format(w=w, t=t), "reminder", {"remind_what": w, "remind_when": t})
    for _ in range(50):
        n = R.choice([1, 2, 3, 5, 10, 15, 20, 25, 30, 45])
        u = R.choice(TIMER_T).format(n=n)
        when = next(iter(brain.reminder_candidates(u)[1].values()))
        emit(R.choice(PLEASE) + u, "reminder", {"remind_when": when})

    for _ in range(9):
        for q in QUESTIONS:
            emit(R.choice(PLEASE) + q, "question")
    for _ in range(18):
        for s in STOP:
            emit(R.choice(PLEASE[:3] + ["jarvis "]) + s, "stop")
    for _ in range(9):
        for s in NONE:
            emit(s, "none")

    # compound: two single commands joined; label = the first command's action
    singles = [(f"open {a}", "open_app") for a, _ in SPOKEN_APPS[:30]] + \
              [(f"type {t}", "type_text") for t in TYPE_TEXTS[:12]] + \
              [(f"go to {s[0]}", "open_website") for s in list(SITE_SPOKEN.values())[:10]] + \
              [(u, "volume") for u, _ in VOLUME[:10]] + [(u, "media") for u, _ in MEDIA[:10]] + \
              [(s[0], "shortcut") for s in list(SHORTCUT_SPOKEN.values())[:16]]
    for _ in range(420):
        (u1, a1), (u2, _) = R.sample(singles, 2)
        emit(R.choice(PLEASE) + u1 + R.choice([" and ", " and then ", " then ", ", then "]) + u2, a1, compound=True)


def main() -> None:
    gen()
    R.shuffle(rows)
    out = HERE / "data"
    out.mkdir(exist_ok=True)
    with (out / "train.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    by_q = Counter(r["qid"] for r in rows)
    by_action = Counter(r["gold"] for r in rows if r["qid"] == "action")
    print(f"{len(rows)} rows -> {out / 'train.jsonl'}")
    print("per question:", dict(by_q))
    print("per action:", dict(by_action))
    print("dropped:", dict(dropped))


if __name__ == "__main__":
    main()
