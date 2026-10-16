"""The blind A/B review page's pairing and vote records (scripts/review_answers.py, PLAN 4.13 #8)."""
import json
import random
import sys
import threading
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import review_answers as ra  # noqa: E402


def _rows(finals, **extra):
    """Rows the way eval_jarvis_tasks writes them: seed after seed."""
    return [{"id": t, "group": "web", "final": f, "success": True, "calls": ["research"], "seconds": 2.0, **extra}
            for t, f in finals]


def _files(tmp_path, a_rows, b_rows):
    a, b = tmp_path / "qwen_r3.json", tmp_path / "defiant_r3.json"
    a.write_text(json.dumps(a_rows), encoding="utf-8")
    b.write_text(json.dumps(b_rows), encoding="utf-8")
    return a, b


TURNS = {"bleach": ["Do you know that Bleach is getting a new arc?"], "len": ["keep it 200", "what was it?"],
         "same": ["x"]}


def test_pairs_match_task_and_seed_and_skip_identical(tmp_path):
    a, b = _files(tmp_path,
                  _rows([("bleach", "A0"), ("same", "same  answer"), ("bleach", "A1")]),
                  _rows([("bleach", "B0"), ("same", "same answer"), ("bleach", "B1")]))
    pairs = ra.build_pairs(a, b, TURNS, set(), random.Random(1))
    got = {(p.task, p.rep): (p.answers["qwen_r3"]["final"], p.answers["defiant_r3"]["final"]) for p in pairs}
    assert got == {("bleach", 0): ("A0", "B0"), ("bleach", 1): ("A1", "B1")}  # whitespace-identical skipped
    assert len({p.key for p in pairs}) == 2


def test_pair_keys_are_stable_and_voted_pairs_are_skipped(tmp_path):
    a, b = _files(tmp_path, _rows([("bleach", "A0")]), _rows([("bleach", "B0")]))
    [p] = ra.build_pairs(a, b, TURNS, set(), random.Random(1))
    [q] = ra.build_pairs(b, a, TURNS, set(), random.Random(7))  # file order doesn't matter
    assert p.key == q.key
    assert ra.build_pairs(a, b, TURNS, {p.key}, random.Random(1)) == []


def test_sides_are_random(tmp_path):
    a, b = _files(tmp_path, _rows([("bleach", f"A{i}") for i in range(40)]),
                  _rows([("bleach", f"B{i}") for i in range(40)]))
    lefts = {p.left for p in ra.build_pairs(a, b, TURNS, set(), random.Random(3))}
    assert lefts == {"qwen_r3", "defiant_r3"}


def test_vote_records_are_dpo_shaped(tmp_path):
    a, b = _files(tmp_path, _rows([("len", "It was 200.")]), _rows([("len", "It was 500.")]))
    [p] = ra.build_pairs(a, b, TURNS, set(), random.Random(1))
    left_text = p.answers[p.left]["final"]
    rec = ra.vote_record(p, "right")
    assert rec["prompt"] == "what was it?" and rec["earlier_turns"] == ["keep it 200"]
    assert rec["rejected"] == left_text and rec["chosen"] != left_text
    assert rec["chosen_model"] == p.right and not rec["tie"]
    assert ra.vote_record(p, "tie")["tie"] and ra.vote_record(p, "skip") is None


def test_the_page_never_sees_the_models_before_the_vote(tmp_path):
    a, b = _files(tmp_path, _rows([("bleach", "A0"), ("len", "A1")]), _rows([("bleach", "B0"), ("len", "B1")]))
    votes = tmp_path / "votes.jsonl"
    review = ra.Review(ra.build_pairs(a, b, TURNS, set(), random.Random(1)), votes_path=votes)
    server = ra.ThreadingHTTPServer(("127.0.0.1", 0), ra.make_handler(review))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        def post(data):
            req = Request(base + "/api/vote", json.dumps(data).encode(), {"Content-Type": "application/json"})
            return json.loads(urlopen(req).read())

        assert b"Which answer is better" in urlopen(base + "/").read()
        cur = json.loads(urlopen(base + "/api/pair").read())
        assert "qwen" not in json.dumps(cur) and "defiant" not in json.dumps(cur)
        res = post({"key": cur["key"], "choice": "left"})
        assert {res["reveal"]["left"]["model"], res["reveal"]["right"]["model"]} == {"qwen_r3", "defiant_r3"}
        assert post({"key": cur["key"], "choice": "left"})["error"]  # a double click doesn't vote twice
        nxt = json.loads(urlopen(base + "/api/pair").read())
        post({"key": nxt["key"], "choice": "skip"})
        assert json.loads(urlopen(base + "/api/pair").read())["done"]
    finally:
        server.shutdown()
    [line] = votes.read_text(encoding="utf-8").splitlines()  # the skip saved nothing
    assert json.loads(line)["pair"] == cur["key"] and ra.voted_keys(votes) == {cur["key"]}


def test_eval_tasks_give_their_turns():
    turns = ra.task_turns()
    assert turns["bleach-arc"] == ["Do you know that Bleach is getting a new arc?"]
    assert len(turns["prompt-length"]) == 3
