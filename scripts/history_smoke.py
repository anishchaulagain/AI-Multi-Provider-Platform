"""Phase 5 check against a running stack (API, gateway, worker, LiteLLM):

1. A 40-turn scripted chat saves more than 60% of history tokens.
2. The model still answers a question about turn 2 correctly at turn 40.
3. A capsule forked onto a different model still knows the earlier facts.

    cd apps/backend && uv run python ../../scripts/history_smoke.py
"""

import argparse
import json
import re
import sys
import time

import httpx

FACT_TURN = "Please remember this: my project codename is BLUEFIN and our launch date is March 14."
PROBE = "What was my project codename and our launch date?"
EXPECT = ("bluefin", "march 14")
FILLER = [
    "How many vacation days do full-time employees get?",
    "Can unused vacation days be carried over?",
    "How long is parental leave at Northwind?",
    "How many days per week can employees work remotely?",
    "What equipment do new engineers get?",
    "How much weight can the Atlas robot lift?",
    "How many items per hour does Atlas pick?",
    "How long does the Courier battery last?",
    "What positioning system does Courier use?",
    "Which language is Helm written in?",
    "How often are firmware updates released?",
    "What is the support response time for critical incidents?",
]


def normalize(text: str) -> str:
    """Lower-case, markdown stripped, any Unicode space (e.g. U+202F) as one space."""
    return re.sub(r"\s+", " ", text.replace("*", "")).lower()


def sse_events(text: str) -> list[tuple[str, dict[str, object]]]:
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


def ask(api: httpx.Client, cid: str, message: str) -> tuple[str, dict[str, object]]:
    for attempt in range(3):
        response = api.post(f"/conversations/{cid}/chat", json={"message": message})
        if response.status_code == 200:
            events = sse_events(response.text)
            answer = "".join(str(d["text"]) for e, d in events if e == "token")
            usage = next((d for e, d in events if e == "usage"), {})
            return answer, usage
        print(f"  retry ({response.status_code}: {response.text[:120]})")
        time.sleep(10 * (attempt + 1))
    raise SystemExit(f"chat failed: {response.status_code} {response.text[:300]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--email", default="admin@example.com")
    parser.add_argument("--password", default="admin12345")
    parser.add_argument("--turns", type=int, default=40)
    parser.add_argument("--fork-alias", default="chat-fast")
    parser.add_argument("--pause", type=float, default=2.0, help="seconds between turns")
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="ask for explanations (typical chat-length answers instead of one-liners)",
    )
    args = parser.parse_args()

    api = httpx.Client(base_url=f"{args.api}/api/v1", timeout=300)
    login = api.post("/auth/login", json={"email": args.email, "password": args.password})
    login.raise_for_status()
    api.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

    cid = api.post("/conversations", json={"title": "History smoke test"}).json()["id"]
    script = ["Hi! I'm planning a warehouse rollout and will ask about Northwind.", FACT_TURN]
    prefix = "In a short paragraph, explain: " if args.detailed else ""
    script += [
        f"{prefix}{FILLER[i % len(FILLER)]} (site {i // len(FILLER) + 1})"
        for i in range(args.turns - 3)
    ]
    script.append(PROBE)

    raw_total = sent_total = 0
    answer = ""
    last: dict[str, object] = {}
    for turn, message in enumerate(script, start=1):
        answer, usage = ask(api, cid, message)
        history = usage.get("history", {}) or {}
        raw, sent = (
            int(history.get("raw_history_tokens", 0)),
            int(history.get("sent_history_tokens", 0)),
        )
        last = history
        raw_total += raw
        sent_total += sent
        print(
            f"turn {turn:2d}: raw={raw:5d} sent={sent:5d} "
            f"recent={history.get('recent_turns')} recalled={history.get('recalled_turns')} "
            f"| {answer[:70]!r}"
        )
        time.sleep(args.pause)

    cumulative = 1 - sent_total / raw_total if raw_total else 0.0
    # The metric the platform reports per request (history_tokens_saved_ratio).
    saved = float(last.get("history_tokens_saved_ratio", 0.0))  # type: ignore[arg-type]
    probe_ok = all(e in normalize(answer) for e in EXPECT)
    print(
        f"\nhistory tokens at turn {len(script)}: raw={last.get('raw_history_tokens')} "
        f"sent={last.get('sent_history_tokens')} saved={saved:.1%} "
        f"(cumulative over all turns, incl. warm-up: {cumulative:.1%})"
    )
    print(f"turn-{len(script)} probe: {'ok' if probe_ok else 'FAIL'} -> {answer!r}")

    capsule = api.post(f"/conversations/{cid}/capsules", json={})
    capsule.raise_for_status()
    capsule_data = capsule.json()
    summary = capsule_data["content"]["summary"][:160]
    print(f"capsule: {capsule_data['token_count']} tokens, summary={summary!r}")
    fork = api.post(f"/capsules/{capsule_data['id']}/fork", json={"alias": args.fork_alias})
    fork.raise_for_status()
    fork_answer, fork_usage = ask(api, fork.json()["id"], PROBE)
    gateway = fork_usage.get("gateway", {}) or {}
    fork_ok = all(e in normalize(fork_answer) for e in EXPECT)
    print(
        f"fork on {args.fork_alias} ({gateway.get('deployment')}): "
        f"{'ok' if fork_ok else 'FAIL'} -> {fork_answer!r}"
    )

    checks = {
        "saved > 60% at turn 40": saved > 0.6,
        "turn-2 probe": probe_ok,
        "forked capsule": fork_ok,
    }
    print("\n" + "\n".join(f"{'PASS' if ok else 'FAIL'}  {name}" for name, ok in checks.items()))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
