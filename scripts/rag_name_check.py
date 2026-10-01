"""
Name-lookup check for a workspace's RAG (PLAN §4.8a step 1).

For every question in a test set it asks the running backend where the expected answer was
lost -- never indexed, not found by the semantic or keyword search, cut by the reranker, or
delivered to the model -- and, with --ask, also puts the question to the model in an
off-the-record turn (nothing saved) and checks the answer.

Test set: a JSON list of {"question": ..., "expect": ...}; "expect" is the exact name as the
document spells it.

    .venv\\Scripts\\python.exe scripts\\rag_name_check.py --project "My Workspace" --tests data\\rag_name_tests.json
    .venv\\Scripts\\python.exe scripts\\rag_name_check.py --project "My Workspace" --tests data\\rag_name_tests.json --ask

Needs Jarvis running (the backend holds the index open). Writes the full results, every
passage included, to backend/data/rag_checks/ and prints a summary table.
"""
import argparse
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def find_project(client: httpx.Client, wanted: str) -> dict:
    projects = client.get("/api/projects").raise_for_status().json()
    for p in projects:
        if wanted in (p.get("id"), p.get("name")):
            return p
    matches = [p for p in projects if wanted.lower() in (p.get("name") or "").lower()]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(repr(p.get("name")) for p in projects)
    sys.exit(f"No single workspace matches {wanted!r}. Workspaces: {names}")


def ask(client: httpx.Client, project_id: str, question: str) -> dict:
    """One off-the-record workspace turn; the answer, the model that gave it, tools it ran."""
    body = {"message": question, "session_id": f"eph-ragcheck-{uuid.uuid4().hex[:8]}",
            "chat_mode": "WORKSPACE", "project_id": project_id, "ephemeral": True}
    done, error, event = None, None, None
    with client.stream("POST", "/chat/stream", json=body, timeout=600) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: ") and event in ("done", "error", "confirmation_required"):
                data = json.loads(line[6:])
                if event == "done":
                    done = data
                else:
                    error = f"{event}: {data}"
    client.delete(f"/sessions/{body['session_id']}")
    if done is None:
        return {"answer": None, "error": error or "no answer"}
    return {"answer": done.get("response") or "", "model": done.get("model"),
            "tools_used": [t.get("tool") if isinstance(t, dict) else t for t in done.get("tools_used") or []]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="workspace name (or part of it) or id")
    ap.add_argument("--tests", required=True, type=Path, help="JSON list of {question, expect}")
    ap.add_argument("--ask", action="store_true", help="also ask the model each question")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    from app.rag.trace import names_not_in

    tests = json.loads(args.tests.read_text(encoding="utf-8"))
    client = httpx.Client(base_url=args.base, timeout=120)
    try:
        project = find_project(client, args.project)
    except httpx.ConnectError as e:
        sys.exit(f"Can't reach the backend at {args.base} ({e}).\n"
                 "Start Jarvis first. If the error is WinError 10013, the socket was blocked: run this "
                 "from your own terminal, not from a sandboxed one (e.g. a Run button).")
    print(f"Workspace: {project['name']} ({project['id']}), {len(tests)} questions\n")

    results = []
    for i, t in enumerate(tests, 1):
        q, expect = t["question"], t["expect"]
        trace = client.post("/api/rag/trace", json={"project_id": project["id"], "query": q,
                                                    "expect": expect}).raise_for_status().json()
        row = {"question": q, "expect": expect, "verdict": trace["expect"]["verdict"],
               "stages": trace["expect"]["stages"], "trace": trace}
        line = f"{i:>2}. {q}\n    expect {expect!r}: {row['verdict']}"
        if args.ask:
            reply = ask(client, project["id"], q)
            row["reply"] = reply
            if reply.get("answer") is not None:
                answer = reply["answer"]
                # every word of the expected answer, in any order ("tournament in Chapter 2")
                words = set(re.findall(r"[\w'-]+", answer.lower()))
                row["answer_has_expect"] = (expect.lower() in answer.lower()
                                            or all(w in words for w in expect.lower().split()))
                row["names_not_in_passages"] = names_not_in(answer, [{"content": c["text"]} for c in trace["sent_to_model"]])
                line += (f"\n    model {reply.get('model')}: {'RIGHT' if row['answer_has_expect'] else 'WRONG'}"
                         f" -- {' '.join(answer.split())[:160]}")
                if row["names_not_in_passages"]:
                    line += f"\n    names not in the passages: {', '.join(row['names_not_in_passages'])}"
                if reply.get("tools_used"):
                    line += f"\n    tools: {', '.join(map(str, reply['tools_used']))}"
            else:
                line += f"\n    model: no answer ({reply.get('error')})"
        print(line + "\n")
        results.append(row)

    retrieved = sum(r["stages"]["final"] for r in results)
    print(f"Retrieval delivered the expected name for {retrieved} of {len(results)} questions.")
    if args.ask:
        right = sum(bool(r.get("answer_has_expect")) for r in results)
        print(f"The model's answer contained it for {right} of {len(results)}.")
    out_dir = ROOT / "backend" / "data" / "rag_checks"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"project": project, "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Full results (every passage): {out}")


if __name__ == "__main__":
    main()
