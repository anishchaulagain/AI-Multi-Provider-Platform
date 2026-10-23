import asyncio
import json
import uuid

import aio_pika
import pymupdf
from minio import Minio
from qdrant_client import AsyncQdrantClient

from integration.conftest import ENV, TAG, Stack
from platform_rag.store import ChunkStore


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=10)
    data: bytes = doc.tobytes()
    doc.close()
    return data


PDF = make_pdf(
    [
        "Retrieval augmented generation combines search with language models. " * 8,
        "Chunks are embedded and stored in a vector database for hybrid search. " * 8,
    ]
)


async def test_pdf_upload_reaches_ready_with_chunks_in_qdrant(stack: Stack) -> None:
    await stack.start_worker()

    response = await stack.upload("guide.pdf", PDF)
    assert response.status_code == 201, response.text
    doc = response.json()
    assert doc["status"] == "queued"

    # Live status over SSE (the per-document stream closes once the doc is ready).
    events_response = await stack.client.get(
        f"/api/v1/documents/{doc['id']}/events", headers=stack.headers, timeout=30
    )
    statuses = [
        json.loads(line[6:])["status"]
        for line in events_response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert statuses[-1] == "ready"

    ready = await stack.wait_for(doc["id"], {"ready"})
    assert ready["page_count"] == 2
    assert ready["chunk_count"] >= 2
    assert ready["attempts"] == 1
    assert ready["details"]["summary"] == "A test document."
    graph = ready["details"]["graph"]
    assert graph["status"] == "ok" and graph["entities"] == 2 and graph["relations"] == 1
    assert ready["ingested_at"] is not None

    chunks = (
        await stack.client.get(
            f"/api/v1/documents/{doc['id']}/chunks?limit=100", headers=stack.headers
        )
    ).json()
    assert len(chunks["items"]) == ready["chunk_count"] == chunks["total"]
    assert {c["page"] for c in chunks["items"]} == {1, 2}
    assert "Retrieval augmented generation" in chunks["items"][0]["text"]
    # The gateway was called on behalf of the uploading tenant.
    assert stack.gateway.tenants == {(await _me(stack))["tenant_id"]}


async def test_duplicate_upload_returns_existing_document(stack: Stack) -> None:
    first = await stack.upload("a.txt", b"Same content twice.")
    second = await stack.upload("renamed.txt", b"Same content twice.")
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["x-duplicate"] == "true"
    assert second.json()["id"] == first.json()["id"]


async def test_upload_validation(stack: Stack) -> None:
    assert (await stack.upload("x.exe", b"MZ...")).status_code == 415
    assert (await stack.upload("x.pdf", b"not a pdf")).status_code == 415
    assert (await stack.upload("empty.txt", b"")).status_code == 400


async def test_corrupt_file_fails_and_lands_in_dlq(stack: Stack) -> None:
    await stack.start_worker()
    # Passes the upload sniff (PDF header) but cannot be parsed.
    response = await stack.upload("broken.pdf", b"%PDF-1.7\n" + b"\x00garbage" * 50)
    doc_id = response.json()["id"]

    failed = await stack.wait_for(doc_id, {"failed", "ready"})
    assert failed["status"] == "failed"
    assert "PDF" in failed["error"]
    assert failed["attempts"] == 1  # permanent: not retried

    dead = await _dlq_message(doc_id)
    assert dead.headers["x-error-type"] == "PermanentError"
    assert "PDF" in str(dead.headers["x-error"])


async def test_transient_gateway_failure_is_retried(stack: Stack) -> None:
    # The gateway client retries 503s itself (3 tries); fail all of them so the
    # job-level retry (via the TTL retry queue) kicks in.
    stack.gateway.embed_failures = 3
    await stack.start_worker()
    doc_id = (await stack.upload("notes.md", b"# Notes\n\nSome notes to embed.")).json()["id"]

    ready = await stack.wait_for(doc_id, {"ready", "failed"})
    assert ready["status"] == "ready"
    assert ready["attempts"] == 2
    assert ready["error"] is None


async def test_worker_killed_mid_job_is_redelivered(stack: Stack) -> None:
    stack.gateway.hang_first_embed = True
    crashed = await stack.start_worker()
    doc_id = (await stack.upload("crash.txt", b"Text that will be processed twice.")).json()["id"]
    await stack.wait_for(doc_id, {"embedding"})

    # The worker "dies" holding the unacked job: drop its broker connection.
    assert crashed.consumer._connection is not None
    await crashed.consumer._connection.close()

    await stack.start_worker()
    ready = await stack.wait_for(doc_id, {"ready"})
    assert ready["chunk_count"] == 1


async def test_delete_removes_chunks_and_file(stack: Stack) -> None:
    await stack.start_worker()
    doc_id = (await stack.upload("gone.txt", b"Delete me after ingestion.")).json()["id"]
    await stack.wait_for(doc_id, {"ready"})

    response = await stack.client.delete(f"/api/v1/documents/{doc_id}", headers=stack.headers)
    assert response.status_code == 204
    assert (
        await stack.client.get(f"/api/v1/documents/{doc_id}", headers=stack.headers)
    ).status_code == 404

    qdrant = AsyncQdrantClient(url=ENV["qdrant_url"])
    me = await _me(stack)
    count = await ChunkStore(qdrant, TAG).count(uuid.UUID(me["tenant_id"]), uuid.UUID(doc_id))
    await qdrant.close()
    assert count == 0

    endpoint = ENV["s3_endpoint"].split("://", 1)[1]
    minio = Minio(endpoint, ENV["s3_access_key"], ENV["s3_secret_key"], secure=False)
    objects = await asyncio.to_thread(
        lambda: list(minio.list_objects(TAG, prefix=f"{me['tenant_id']}/{doc_id}/", recursive=True))
    )
    assert objects == []


async def test_reingest_after_failure(stack: Stack) -> None:
    stack.gateway.embed_failures = 99  # exhaust both retries
    await stack.start_worker()
    doc_id = (await stack.upload("retry.txt", b"Eventually this works.")).json()["id"]
    failed = await stack.wait_for(doc_id, {"failed"})
    assert failed["attempts"] == 3

    stack.gateway.embed_failures = 0
    response = await stack.client.post(
        f"/api/v1/documents/{doc_id}/reingest", headers=stack.headers
    )
    assert response.status_code == 202
    ready = await stack.wait_for(doc_id, {"ready"})
    assert ready["attempts"] == 1


async def _me(stack: Stack) -> dict[str, str]:
    me: dict[str, str] = (await stack.client.get("/api/v1/auth/me", headers=stack.headers)).json()
    return me


async def _dlq_message(doc_id: str) -> aio_pika.abc.AbstractIncomingMessage:
    connection = await aio_pika.connect(ENV["rabbitmq_url"])
    async with connection:
        channel = await connection.channel()
        queue = await channel.get_queue(f"{TAG}.dlq")
        for _ in range(100):
            message = await queue.get(no_ack=True, fail=False)
            if message is None:
                await asyncio.sleep(0.1)
                continue
            if json.loads(message.body)["doc_id"] == doc_id:
                return message
    raise AssertionError("no DLQ message for document")


async def test_graph_is_written_and_removed_with_the_document(stack: Stack) -> None:
    await stack.start_worker()
    doc_id = (await stack.upload("graph.txt", b"Retrieval uses vectors.")).json()["id"]
    await stack.wait_for(doc_id, {"ready"})

    entities = (
        await stack.client.get("/api/v1/graph/entities?q=retrieval", headers=stack.headers)
    ).json()["items"]
    assert [e["name"] for e in entities] == ["Retrieval"]
    hood = (
        await stack.client.get(
            f"/api/v1/graph/neighborhood/{entities[0]['id']}", headers=stack.headers
        )
    ).json()
    assert {n["name"] for n in hood["nodes"]} == {"Retrieval", "Vectors"}
    assert hood["edges"][0]["type"] == "USES"

    await stack.client.delete(f"/api/v1/documents/{doc_id}", headers=stack.headers)
    after = (
        await stack.client.get("/api/v1/graph/entities?q=retrieval", headers=stack.headers)
    ).json()["items"]
    assert after == []
