import json
from pathlib import Path

import httpx

from llm_client import Caller, GatewayClient
from platform_graph import extract_graph
from platform_graph.schema import (
    STATEMENTS,
    canonical_entity_type,
    canonical_relation_type,
    entity_id,
    normalize,
)

REPO_ROOT = Path(__file__).resolve().parents[5]
CALLER = Caller(tenant_id="00000000-0000-0000-0000-000000000001")


def test_init_cypher_matches_code() -> None:
    shipped = (REPO_ROOT / "infra" / "neo4j" / "init.cypher").read_text()
    statements = [
        line.rstrip(";") for line in shipped.splitlines() if line and not line.startswith("//")
    ]
    assert statements == list(STATEMENTS), (
        "Regenerate: uv run python -m platform_graph.schema > infra/neo4j/init.cypher"
    )


def test_entity_resolution_keys() -> None:
    assert normalize("  The  OpenAI, Inc. ") == "openai inc"
    assert entity_id("t", "Organization", "OpenAI Inc.") == entity_id(
        "t", "Organization", "openai inc"
    )
    assert entity_id("t", "Organization", "Qdrant") != entity_id("t", "Product", "Qdrant")
    assert entity_id("t1", "Product", "Qdrant") != entity_id("t2", "Product", "Qdrant")


def test_type_coercion() -> None:
    assert canonical_entity_type("person") == "Person"
    assert canonical_entity_type("Company") == "Organization"
    assert canonical_entity_type("weird") == "Concept"
    assert canonical_relation_type("works for") == "WORKS_FOR"
    assert canonical_relation_type("loves") == "RELATED_TO"


class FakeExtractor:
    def __init__(self, replies: list[object]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.prompts.append(body["messages"][-1]["content"])
        reply = self.replies.pop(0)
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"code": "x", "message": "down"}})
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
        )


def _client(fake: FakeExtractor) -> GatewayClient:
    return GatewayClient(
        "http://gw", "t", transport=httpx.MockTransport(fake.handler), max_retries=0
    )


async def test_extraction_cleans_and_batches() -> None:
    fake = FakeExtractor(
        [
            {
                "chunks": [
                    {
                        "chunk": 0,
                        "entities": [
                            {"name": "Qdrant", "type": "product"},
                            {"name": "  Rust ", "type": "technology"},
                            {"name": "x", "type": "Concept"},  # too short
                            {"name": "qdrant", "type": "Product"},  # duplicate
                        ],
                        "relations": [
                            {"source": "Qdrant", "type": "uses", "target": "Rust"},
                            {"source": "Qdrant", "type": "uses", "target": "Unknown"},
                            {"source": "Qdrant", "type": "uses", "target": "qdrant"},
                        ],
                    },
                    {"chunk": 99, "entities": [{"name": "Ignored", "type": "Concept"}]},
                ]
            },
            {"chunks": [{"chunk": 2, "entities": [{"name": "Neo4j", "type": "Product"}]}]},
        ]
    )
    chunks = [(0, "Qdrant is written in Rust. " * 10), (2, "Neo4j stores graphs. " * 10)]
    result = await extract_graph(_client(fake), CALLER, chunks, batch_chars=300)

    assert len(fake.prompts) == 2  # 300-char batches split the two chunks
    assert "[chunk 0]" in fake.prompts[0] and "[chunk 2]" in fake.prompts[1]
    assert [(e.name, e.type) for e in result.mentions[0]] == [
        ("Qdrant", "Product"),
        ("Rust", "Technology"),
    ]
    assert 99 not in result.mentions
    [(relation, index)] = result.relations
    assert (relation.source.name, relation.type, relation.target.name, index) == (
        "Qdrant",
        "USES",
        "Rust",
        0,
    )
    assert result.entity_count == 3
    assert result.failed_batches == 0


async def test_failed_batches_are_skipped() -> None:
    fake = FakeExtractor([503, "not json"])
    result = await extract_graph(
        _client(fake), CALLER, [(0, "a" * 50), (1, "b" * 50)], batch_chars=60
    )
    assert result.failed_batches == 2
    assert result.mentions == {}
