import json
from pathlib import Path

from platform_messaging import tracing
from platform_messaging.topology import INGEST_DOCUMENTS, WorkQueue, definitions

REPO_ROOT = Path(__file__).resolve().parents[5]
REGENERATE = "uv run python -m platform_messaging.topology > infra/rabbitmq/definitions.json"


def test_definitions_file_matches_code() -> None:
    shipped = json.loads((REPO_ROOT / "infra" / "rabbitmq" / "definitions.json").read_text())
    assert shipped == definitions(), f"Regenerate with: {REGENERATE}"


def test_ingest_queue_topology() -> None:
    q = INGEST_DOCUMENTS
    assert [name for name, _ in q.retry_queues] == [
        "ingest.documents.retry.10s",
        "ingest.documents.retry.1m",
        "ingest.documents.retry.10m",
    ]
    assert q.queue_arguments()["x-dead-letter-exchange"] == "ingest.dlx"
    assert q.queue_arguments()["x-queue-type"] == "quorum"
    assert q.retry_queue_arguments(10_000) == {
        "x-message-ttl": 10_000,
        "x-dead-letter-exchange": "ingest",
        "x-dead-letter-routing-key": "documents.ingest",
    }


def test_delay_labels() -> None:
    q = WorkQueue("w", "x", "k", retry_delays_ms=(500, 2_000, 120_000))
    assert [n for n, _ in q.retry_queues] == ["w.retry.500ms", "w.retry.2s", "w.retry.2m"]


def test_traceparent_propagation() -> None:
    parent = tracing.new_traceparent()
    child = tracing.child_traceparent(parent)
    assert tracing.trace_id(child) == tracing.trace_id(parent)
    assert child != parent
    assert tracing.trace_id(tracing.child_traceparent("garbage")) is not None
