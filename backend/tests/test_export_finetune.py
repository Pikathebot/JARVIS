"""PLAN 4.13 #10: the fine-tune export -- clean DPO pairs from votes, a review list from the training log, redaction."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import export_finetune_data as ex  # noqa: E402


def _jsonl(path: Path, rows: list) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8")
    return path


def _vote(pair, chosen="Done.", rejected="I have completed the task you asked for.", **kw):
    return {"pair": pair, "prompt": "Turn the volume to 30", "earlier_turns": [], "chosen": chosen, "rejected": rejected,
            "tie": False, "source": "review-page", "chosen_model": "a", "rejected_model": "b", **kw}


def test_votes_become_conversational_dpo_pairs_and_bad_ones_are_dropped(tmp_path):
    votes = _jsonl(tmp_path / "v.jsonl", [
        _vote("p1"),
        _vote("p2", tie=True),
        _vote("p3", chosen="same", rejected="same"),
        _vote("p4", earlier_turns=["first message"]),
        _vote("p5", chosen_passed=False, rejected_passed=True),
        _vote("p6", chosen_passed=True, rejected_passed=False),
        _vote("p7", rejected=""),
        _vote("p1", chosen="Volume 30.", rejected="Done."),       # re-vote on p1 replaces the first
        {**_vote("c1"), "source": "in-app"},
    ])
    m = ex.export(votes, tmp_path / "none.jsonl", tmp_path / "out")
    rows = [json.loads(l) for l in (tmp_path / "out" / "dpo.jsonl").read_text().splitlines()]
    assert sorted(r["id"] for r in rows) == ["c1", "p1", "p6"]
    p1 = next(r for r in rows if r["id"] == "p1")
    assert p1["prompt"] == [{"role": "user", "content": "Turn the volume to 30"}]
    assert p1["chosen"] == [{"role": "assistant", "content": "Volume 30."}] and p1["rejected"][0]["content"] == "Done."
    assert m["dpo_pairs"] == 3 and m["dpo_by_source"] == {"review-page": 2, "in-app": 1}
    assert m["dropped_votes"] == {"tie": 1, "identical answers": 1, "multi-turn (answers between turns not stored)": 1,
                                  "chosen failed the check, rejected passed": 1, "empty side or prompt": 1}


def test_the_log_becomes_a_review_list_with_flags_resolved(tmp_path):
    turn = lambda tid, nets=("claim-checked",): {"turn_id": tid, "model": "main", "user_message": "remind me at 5",
                                                 "reply": "Reminder set for 5 PM.", "tools": [], "nets": list(nets),
                                                 "sent_back": [{"net": "claim-checked", "draft": "Reminder set.", "note": "n"}]}
    log = _jsonl(tmp_path / "log.jsonl", [
        {"kind": "net", **turn("t1")},
        {"kind": "net", **turn("t2")},
        {"kind": "wrong", **turn("t2"), "meant": "first"},
        {"kind": "wrong", **turn("t2"), "meant": "5 in the evening"},
        {"kind": "wrong", **turn("t3"), "meant": "x"},
        {"kind": "unflagged", "turn_id": "t3"},
        {"kind": "capture", "net": "memory", "saved": "likes tea"},
    ])
    m = ex.export(tmp_path / "none.jsonl", log, tmp_path / "out")
    rows = [json.loads(l) for l in (tmp_path / "out" / "review.jsonl").read_text().splitlines()]
    assert [(r["kind"], r["turn_id"]) for r in rows] == [("net", "t1"), ("net", "t2"), ("wrong", "t2")]
    assert rows[2]["meant"] == "5 in the evening" and rows[1]["kept_reply_flagged_wrong"] and "kept_reply_flagged_wrong" not in rows[0]
    assert m["review_rows"] == {"net": 2, "wrong": 1} and m["captures_in_log"] == 1 and m["dpo_pairs"] == 0
    assert (tmp_path / "out" / "dpo.jsonl").read_text() == ""


def test_personal_details_are_redacted_everywhere(tmp_path):
    secret = ("Mail kiran.k@example.com or call +91 98765 43210 about C:\\Users\\Arjun\\Desktop\\notes.md; "
              "Kiran and girija said hi. Version 1.4.4, 2026-10-20, 8188 MiB stay.")
    votes = _jsonl(tmp_path / "v.jsonl", [_vote("p1", chosen=secret)])
    log = _jsonl(tmp_path / "l.jsonl", [{"kind": "net", "turn_id": "t", "user_message": secret, "reply": "",
                                         "nets": ["nudged"], "tools": [{"tool": "read_file", "args": {"path": secret}}],
                                         "sent_back": []}])
    m = ex.export(votes, log, tmp_path / "out", names=["Kiran", " Girija ", ""])
    out = (tmp_path / "out" / "dpo.jsonl").read_text() + (tmp_path / "out" / "review.jsonl").read_text()
    for leak in ("kiran", "Kiran", "girija", "98765", "Arjun", "example.com"):
        assert leak not in out, leak
    for kept in ("<email>", "<phone>", "C:\\\\Users\\\\<user>\\\\Desktop", "<name> and <name>", "1.4.4", "2026-10-20", "8188 MiB"):
        assert kept in out, kept
    assert m["redactions"]["email"] == 3 and m["redactions"]["name"] == 6


def test_missing_inputs_give_an_empty_export(tmp_path):
    m = ex.export(tmp_path / "a", tmp_path / "b", tmp_path / "out")
    assert m["dpo_pairs"] == 0 and m["review_rows"] == {} and (tmp_path / "out" / "manifest.json").exists()


def test_in_app_votes_carry_their_context_and_old_ones_are_counted(tmp_path):
    ctx = [{"role": "user", "content": "my sister is Kiran"}, {"role": "assistant", "content": "Noted."}]
    votes = _jsonl(tmp_path / "v.jsonl", [{**_vote("c1", earlier_turns=ctx), "source": "in-app"},
                                          {**_vote("c2"), "source": "in-app"}])
    m = ex.export(votes, tmp_path / "none.jsonl", tmp_path / "out", names=["Kiran"])
    rows = {r["id"]: r for r in map(json.loads, (tmp_path / "out" / "dpo.jsonl").read_text().splitlines())}
    assert rows["c1"]["prompt"] == [{"role": "user", "content": "my sister is <name>"}, {"role": "assistant", "content": "Noted."},
                                    {"role": "user", "content": "Turn the volume to 30"}]
    assert rows["c1"]["context_stored"] and not rows["c2"]["context_stored"]
    assert m["dpo_in_app_without_context"] == 1


def test_compare_keeps_the_plain_turns_before_the_voted_one():
    from app.agent import compare
    msgs = [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"t{i}"} for i in range(9)] + [
        {"role": "tool", "content": "result"}, {"role": "assistant", "content": None}, {"role": "user", "content": "now"}]
    turns = compare.earlier_turns(msgs)
    assert [t["content"] for t in turns] == ["t3", "t4", "t5", "t6", "t7", "t8"] and len(turns) == compare.CONTEXT_TURNS
