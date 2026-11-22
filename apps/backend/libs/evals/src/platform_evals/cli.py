"""Offline evaluation against a running stack (`make eval`).

    uv run python -m platform_evals.cli --suite answers

1. Uploads the golden corpus (evals/corpus) and waits until it is ingested.
2. Asks every question in evals/datasets/golden.jsonl in a fresh conversation and has
   the API's LLM judge score the answer (`POST /evals/messages/{id}`, source=offline).
3. Adds retrieval metrics from the expected sources: hit@k and MRR over the numbered
   sources, expected-term recall, and abstention accuracy (unanswerable questions must
   get "I don't know"; answerable ones must not).
4. With `--suite history` (or `all`), replays the long conversations in
   evals/datasets/history_*.jsonl and checks the probes about early turns.

Writes evals/reports/<run id>.json and exits 1 when a mean is below its minimum in
evals/thresholds.yaml (2 when the run itself failed).
"""

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

import httpx
import yaml

from platform_evals.evaluator import METRICS
from platform_evals.metrics import is_abstention
from platform_evals.retrieval import hit_at_k, reciprocal_rank

ROOT = Path(__file__).resolve().parents[6]
EVALS = ROOT / "evals"


class RunError(Exception):
    pass


def normalize(text: str) -> str:
    """Lower-case, markdown emphasis dropped, curly quotes straightened, any Unicode
    space (e.g. U+202F) as one space."""
    text = text.replace("*", "").replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).lower()


def sse_events(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


@dataclass
class Api:
    client: httpx.Client
    retries: int = 4

    def call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Retries rate limits and provider outages with backoff (free tiers are bursty)."""
        for attempt in range(self.retries + 1):
            response = self.client.request(method, url, **kwargs)
            if response.status_code not in (429, 502, 503, 504) or attempt == self.retries:
                return response
            wait = float(response.headers.get("retry-after") or 0) or 5 * 2**attempt
            print(f"  {response.status_code} on {url}; retrying in {wait:.0f}s")
            time.sleep(min(wait, 60))
        raise AssertionError("unreachable")

    def ok(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self.call(method, url, **kwargs)
        if response.status_code >= 400:
            raise RunError(f"{method} {url}: HTTP {response.status_code} {response.text[:300]}")
        data: dict[str, Any] = response.json()
        return data

    def chat(self, conversation_id: str, message: str) -> dict[str, Any]:
        response = self.call(
            "POST", f"/conversations/{conversation_id}/chat", json={"message": message}
        )
        if response.status_code != 200:
            raise RunError(f"chat: HTTP {response.status_code} {response.text[:300]}")
        events = sse_events(response.text)
        error = next((d for e, d in events if e == "error"), None)
        if error:
            raise RunError(f"chat stream failed: {error}")
        return {
            "answer": "".join(d["text"] for e, d in events if e == "token"),
            "citations": next((d for e, d in events if e == "citations"), {}),
            "usage": next((d for e, d in events if e == "usage"), {}),
            "message_id": next(d for e, d in events if e == "done")["message_id"],
        }


# ---------- corpus ----------
def ingest_corpus(api: Api, timeout: float) -> dict[str, str]:
    """Upload every corpus file (duplicates are no-ops) and wait until ready."""
    doc_ids: dict[str, str] = {}
    for path in sorted((EVALS / "corpus").glob("*.md")):
        doc = api.ok("POST", "/documents", files={"file": (path.name, path.read_bytes())})
        doc_ids[path.name] = doc["id"]
    deadline = time.monotonic() + timeout
    for name, doc_id in doc_ids.items():
        while True:
            doc = api.ok("GET", f"/documents/{doc_id}")
            if doc["status"] == "ready":
                break
            if doc["status"] == "failed":
                raise RunError(f"{name} failed to ingest: {doc['error']}")
            if time.monotonic() > deadline:
                raise RunError(f"timed out waiting for {name} (status {doc['status']})")
            time.sleep(2)
        print(f"corpus: {name} ready ({doc['chunk_count']} chunks)")
    return doc_ids


# ---------- answers suite ----------
@dataclass
class CaseResult:
    id: str
    question: str
    answer: str = ""
    scores: dict[str, float | None] = field(default_factory=dict)
    retrieval: dict[str, float | None] = field(default_factory=dict)
    eval_id: str | None = None
    error: str | None = None


def run_answers(api: Api, doc_ids: dict[str, str], run_id: str, k: int) -> list[CaseResult]:
    results = []
    cases = [
        json.loads(line)
        for line in (EVALS / "datasets" / "golden.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    for case in cases:
        result = CaseResult(id=case["id"], question=case["question"])
        results.append(result)
        try:
            conversation = api.ok(
                "POST", "/conversations", json={"doc_ids": list(doc_ids.values())}
            )
            chat = api.chat(conversation["id"], case["question"])
            result.answer = chat["answer"]
            judged = api.ok(
                "POST",
                f"/evals/messages/{chat['message_id']}",
                json={"source": "offline", "run_id": run_id},
            )
        except RunError as exc:
            result.error = str(exc)
            print(f"ERROR {case['id']}: {exc}")
            continue
        result.eval_id = judged["id"]
        result.scores = {name: judged[name] for name in METRICS}
        result.retrieval = case_retrieval(case, chat["answer"], judged["sources"], doc_ids, k)
        shown = ", ".join(
            f"{n[:5]}={v:.2f}"
            for n, v in {**result.scores, **result.retrieval}.items()
            if v is not None
        )
        print(f"{case['id']}: {shown}")
    return results


def case_retrieval(
    case: dict[str, Any],
    answer: str,
    sources: list[dict[str, Any]],
    doc_ids: dict[str, str],
    k: int,
) -> dict[str, float | None]:
    abstained = is_abstention(answer)
    expected_doc = case.get("expected_doc")
    if not expected_doc:
        return {"abstention_accuracy": float(abstained)}
    expected = {doc_ids[expected_doc]}
    retrieved = [s["doc_id"] for s in sources]
    text = normalize(answer)
    terms = case.get("expected_terms") or []
    return {
        "hit_at_k": hit_at_k(retrieved, expected, k),
        "mrr": reciprocal_rank(retrieved, expected),
        "term_recall": (sum(normalize(t) in text for t in terms) / len(terms) if terms else None),
        "abstention_accuracy": float(not abstained),
    }


# ---------- history suite ----------
def run_history(api: Api) -> list[dict[str, Any]]:
    out = []
    for path in sorted((EVALS / "datasets").glob("history_*.jsonl")):
        for line in path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            conversation = api.ok("POST", "/conversations", json={})
            for n, turn in enumerate(case["turns"], start=1):
                api.chat(conversation["id"], turn)
                print(f"history {case['id']}: turn {n}/{len(case['turns'])}", end="\r")
            print()
            for probe in case["probes"]:
                chat = api.chat(conversation["id"], probe["question"])
                text = normalize(chat["answer"])
                hits = [t for t in probe["expected_terms"] if normalize(t) in text]
                history = chat["usage"].get("history") or {}
                raw = history.get("raw_history_tokens") or 0
                sent = history.get("sent_history_tokens") or 0
                out.append(
                    {
                        "case": case["id"],
                        "question": probe["question"],
                        "answer": chat["answer"],
                        "probe_accuracy": len(hits) / len(probe["expected_terms"]),
                        "history_tokens_saved": 1 - sent / raw if raw else None,
                    }
                )
                print(f"probe: {probe['question']!r} -> {len(hits)}/{len(probe['expected_terms'])}")
    return out


# ---------- report ----------
def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(mean(present), 4) if present else None


def summarize(answers: list[CaseResult], history: list[dict[str, Any]]) -> dict[str, float | None]:
    metrics: dict[str, float | None] = {}
    if answers:
        for name in METRICS:
            metrics[name] = _mean([r.scores.get(name) for r in answers if not r.error])
        for name in ("hit_at_k", "mrr", "term_recall", "abstention_accuracy"):
            metrics[name] = _mean([r.retrieval.get(name) for r in answers if not r.error])
    if history:
        metrics["probe_accuracy"] = _mean([h["probe_accuracy"] for h in history])
        metrics["history_tokens_saved"] = _mean([h["history_tokens_saved"] for h in history])
    return metrics


def check(metrics: dict[str, float | None], minimums: dict[str, float]) -> list[str]:
    failures = []
    for name, minimum in minimums.items():
        if name not in metrics:
            continue  # its suite wasn't run
        value = metrics[name]
        if value is None or value < minimum:
            failures.append(f"{name} = {value} < {minimum}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--email", default="admin@example.com")
    parser.add_argument("--password", default="admin12345")
    parser.add_argument("--suite", choices=["answers", "history", "all"], default="answers")
    parser.add_argument("--thresholds", type=Path, default=EVALS / "thresholds.yaml")
    parser.add_argument("--report-dir", type=Path, default=EVALS / "reports")
    parser.add_argument("--timeout", type=float, default=600, help="ingestion wait, seconds")
    args = parser.parse_args(argv)

    config = yaml.safe_load(args.thresholds.read_text("utf-8"))
    k = int(config.get("k", 5))
    minimums: dict[str, float] = {
        name: float(value)
        for group in ("answers", "retrieval", "history")
        for name, value in (config.get(group) or {}).items()
    }
    run_id = "offline-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    api = Api(httpx.Client(base_url=f"{args.api}/api/v1", timeout=300))
    answers: list[CaseResult] = []
    history: list[dict[str, Any]] = []
    try:
        login = api.ok("POST", "/auth/login", json={"email": args.email, "password": args.password})
        api.client.headers["Authorization"] = f"Bearer {login['access_token']}"
        doc_ids = ingest_corpus(api, args.timeout)
        if args.suite in ("answers", "all"):
            answers = run_answers(api, doc_ids, run_id, k)
        if args.suite in ("history", "all"):
            history = run_history(api)
    except (RunError, httpx.HTTPError) as exc:
        print(f"run failed: {exc}")
        return 2

    metrics = summarize(answers, history)
    failures = check(metrics, minimums)
    errors = [r.id for r in answers if r.error]
    if errors:
        failures.append(f"{len(errors)} case(s) errored: {', '.join(errors)}")

    args.report_dir.mkdir(parents=True, exist_ok=True)
    report = args.report_dir / f"{run_id}.json"
    report.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "suite": args.suite,
                "k": k,
                "metrics": metrics,
                "thresholds": minimums,
                "failures": failures,
                "answers": [r.__dict__ for r in answers],
                "history": history,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\nrun {run_id}  (report: {report})")
    for name, value in metrics.items():
        minimum = minimums.get(name)
        mark = (
            ""
            if minimum is None
            else ("  ok" if value is not None and value >= minimum else "  FAIL")
        )
        floor = "" if minimum is None else f"  (min {minimum})"
        print(f"  {name:22} {'-' if value is None else f'{value:.3f}'}{floor}{mark}")
    if failures:
        print("\nFAILED:\n  " + "\n  ".join(failures))
        return 1
    print("\nall thresholds met")
    return 0


if __name__ == "__main__":
    sys.exit(main())
