"""Phase 4 smoke check against a running stack: upload the sample corpus, ask the
golden questions, and check each answer is grounded with valid citations.

    cd apps/backend && uv run python ../../scripts/golden_smoke.py \
        --api http://localhost:8000 --email admin@example.com --password admin12345

Checks per question: the answer contains the expected terms; it cites at least one
source and every citation points at the expected document (or, for questions the
corpus can't answer, the model says it doesn't know). Needs a working LLM route
(Ollama models or a free-tier key). Phase 6 turns this into scored evaluations.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def sse_events(text: str) -> list[tuple[str, dict[str, object]]]:
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--email", default="admin@example.com")
    parser.add_argument("--password", default="admin12345")
    parser.add_argument("--timeout", type=float, default=600, help="ingestion wait, seconds")
    args = parser.parse_args()

    api = httpx.Client(base_url=f"{args.api}/api/v1", timeout=300)
    token = api.post("/auth/login", json={"email": args.email, "password": args.password})
    token.raise_for_status()
    api.headers["Authorization"] = f"Bearer {token.json()['access_token']}"

    doc_ids: dict[str, str] = {}
    for path in sorted((ROOT / "evals" / "corpus").glob("*.md")):
        response = api.post("/documents", files={"file": (path.name, path.read_bytes())})
        response.raise_for_status()
        doc_ids[path.name] = response.json()["id"]
        print(f"uploaded {path.name} ({'existing' if response.status_code == 200 else 'new'})")

    deadline = time.monotonic() + args.timeout
    for name, doc_id in doc_ids.items():
        while True:
            doc = api.get(f"/documents/{doc_id}").json()
            if doc["status"] in ("ready", "failed"):
                break
            if time.monotonic() > deadline:
                print(f"timed out waiting for {name} (status {doc['status']})")
                return 1
            time.sleep(2)
        graph = doc["details"].get("graph", {})
        print(f"{name}: {doc['status']}, {doc['chunk_count']} chunks, graph={graph}")
        if doc["status"] != "ready":
            print(f"  error: {doc['error']}")
            return 1

    conversation = api.post("/conversations", json={"doc_ids": list(doc_ids.values())}).json()
    by_doc = {v: k for k, v in doc_ids.items()}
    failures = 0
    for line in (ROOT / "evals" / "datasets" / "golden.jsonl").read_text().splitlines():
        case = json.loads(line)
        # A fresh conversation per question: no history leaks between cases.
        cid = api.post("/conversations", json={"doc_ids": conversation["doc_ids"]}).json()["id"]
        response = api.post(f"/conversations/{cid}/chat", json={"message": case["question"]})
        if response.status_code != 200:
            print(f"FAIL {case['id']}: HTTP {response.status_code} {response.text[:200]}")
            failures += 1
            continue
        events = sse_events(response.text)
        answer = "".join(d["text"] for e, d in events if e == "token")
        citations = next((d for e, d in events if e == "citations"), {"citations": [], "invalid": []})
        cited_docs = {by_doc.get(c["doc_id"], "?") for c in citations["citations"]}

        problems = [t for t in case["expected_terms"] if t.lower() not in answer.lower()]
        if case["expected_doc"]:
            if not citations["citations"]:
                problems.append("no citations")
            elif cited_docs != {case["expected_doc"]}:
                problems.append(f"cited {sorted(cited_docs)}")
        if citations["invalid"]:
            problems.append(f"invented citations {citations['invalid']}")

        status = "ok  " if not problems else "FAIL"
        failures += bool(problems)
        print(f"{status} {case['id']}: {answer[:140]!r}" + (f"  <- {problems}" if problems else ""))

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
