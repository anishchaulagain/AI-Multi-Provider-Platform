import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
from qdrant_client import AsyncQdrantClient

from llm_client import Caller, GatewayClient
from platform_history import Memory, TokenCounter, TurnStore, build_capsule, render_capsule
from platform_history.compaction import pair_turns, summarize

COUNTER = TokenCounter()
TENANT = uuid.uuid4()
CONV = uuid.uuid4()


def msg(role: str, content: str, citations: list[dict[str, Any]] | None = None) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        content=content,
        citations=citations or [],
        created_at=datetime.now(UTC),
    )


def test_pair_turns_groups_questions_with_answers() -> None:
    msgs = [
        msg("user", "q1"),
        msg("assistant", "a1 [1]"),
        msg("user", "q2"),
        msg("user", "q3"),
        msg("assistant", "a3"),
    ]
    pairs = pair_turns(CONV, msgs, start_seq=10)
    assert [p.text for p in pairs] == [
        "User: q1\nAssistant: a1",
        "User: q2",
        "User: q3\nAssistant: a3",
    ]
    assert pairs[0].seq == 10 and pairs[0].assistant_msg_id == str(msgs[1].id)


async def test_summarize_parses_patch_and_caps_length() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        reply = {
            "summary": "word " * 1000,
            "memory": {
                "add": [
                    {"category": "entities", "text": "Atlas lifts 15 kg", "source_msg_ids": ["x"]}
                ]
            },
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": json.dumps(reply)}}]},
        )

    gateway = GatewayClient("http://gw", "t", transport=httpx.MockTransport(handler))
    summary, patch = await summarize(
        gateway,
        Caller(tenant_id=TENANT),
        previous="Earlier: hello.",
        memory=Memory(),
        new_messages=[msg("user", "How much can Atlas lift?"), msg("assistant", "15 kg [1].")],
        alias="summarize",
        max_tokens=50,
        counter=COUNTER,
    )
    assert COUNTER.count(summary) <= 50
    assert patch.add[0].text == "Atlas lifts 15 kg"
    prompt = seen[0]["messages"][-1]["content"]
    assert "Earlier: hello." in prompt and "15 kg" in prompt and "[1]" not in prompt
    assert seen[0]["model"] == "summarize"


async def test_turn_store_recall_is_scoped_and_excludes_recent() -> None:
    qdrant = AsyncQdrantClient(":memory:")
    store = TurnStore(qdrant, "test")
    msgs = [msg("user", "q1"), msg("assistant", "a1"), msg("user", "q2"), msg("assistant", "a2")]
    pairs = pair_turns(CONV, msgs, 0)
    await store.add(TENANT, pairs, [[1.0, 0.0], [0.9, 0.1]])
    hits = await store.search(TENANT, CONV, [1.0, 0.0], exclude_msg_ids=set(), limit=2)
    assert [h.content for h in hits] == ["User: q1\nAssistant: a1", "User: q2\nAssistant: a2"]
    hits = await store.search(TENANT, CONV, [1.0, 0.0], exclude_msg_ids={str(msgs[0].id)}, limit=2)
    assert [h.content for h in hits] == ["User: q2\nAssistant: a2"]
    assert await store.search(uuid.uuid4(), CONV, [1.0, 0.0], exclude_msg_ids=set()) == []
    assert await store.search(TENANT, uuid.uuid4(), [1.0, 0.0], exclude_msg_ids=set()) == []
    await store.delete_conversation(TENANT, CONV)
    assert await store.search(TENANT, CONV, [1.0, 0.0], exclude_msg_ids=set()) == []
    await qdrant.close()


def test_capsule_has_summary_facts_key_exchanges_and_refs() -> None:
    ref = {"n": 1, "chunk_id": "c1", "doc_id": "d1"}
    msgs = [
        msg("user", "Early cited question"),
        msg("assistant", "Cited answer [1]", [ref]),
        *[m for i in range(5) for m in (msg("user", f"q{i}"), msg("assistant", f"a{i}"))],
    ]
    memory = Memory.model_validate(
        {"entities": [{"text": "Atlas is a robot", "source_msg_ids": []}]}
    )
    content, tokens = build_capsule(
        title="Robots",
        summary="They discussed robots.",
        memory=memory,
        messages=msgs,
        source_model="gemini-flash",
        counter=COUNTER,
    )
    assert [x.question for x in content.key_exchanges] == ["Early cited question", "q2", "q3", "q4"]
    assert content.key_exchanges[0].answer == "Cited answer"
    assert content.citations == [{"doc_id": "d1", "chunk_id": "c1"}]
    text = render_capsule(content)
    assert "Summary: They discussed robots." in text and "Atlas is a robot" in text
    assert tokens == COUNTER.count(text) and tokens < 600
