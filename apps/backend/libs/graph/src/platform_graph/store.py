"""Tenant-scoped Neo4j access: per-document graph writes, deletes, entity search,
neighborhoods and query-time graph expansion for retrieval."""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncManagedTransaction

from platform_graph.extract import GraphExtraction
from platform_graph.schema import ENTITY_FULLTEXT_INDEX, STATEMENTS, entity_id, normalize

logger = structlog.get_logger(__name__)

# Lucene query syntax characters, stripped from user text before full-text search.
_LUCENE_SPECIAL = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')
_STOP = frozenset(
    "a an and are as at be by can do does for from has have how i in is it its of on or "
    "that the their them there these this to was were what when where which who why will "
    "with you your about between".split()
)


@dataclass(frozen=True)
class GraphChunk:
    chunk_id: str
    index: int
    page: int | None = None


@dataclass(frozen=True)
class EntityHit:
    id: str
    name: str
    type: str
    score: float
    mentions: int = 0


@dataclass(frozen=True)
class Fact:
    source: str
    relation: str
    target: str
    doc_id: str
    weight: int

    def text(self) -> str:
        return f"{self.source} {self.relation.replace('_', ' ').lower()} {self.target}"


@dataclass
class Expansion:
    """Graph context for a query: seed entities, the facts around them, and chunks
    that mention them (best first)."""

    seeds: list[EntityHit] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    chunks: list[tuple[str, float]] = field(default_factory=list)  # (chunk_id, score)


@dataclass
class Neighborhood:
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


def query_terms(text: str, *, max_terms: int = 12) -> list[str]:
    words = _LUCENE_SPECIAL.sub(" ", text).split()
    terms: list[str] = []
    for word in words:
        w = word.lower()
        if len(w) >= 3 and w not in _STOP and w not in terms:
            terms.append(w)
    return terms[:max_terms]


class GraphStore:
    def __init__(self, driver: AsyncDriver, *, database: str | None = None) -> None:
        self._driver = driver
        self._database = database

    @classmethod
    def connect(cls, uri: str, user: str, password: str, **kwargs: Any) -> "GraphStore":
        return cls(AsyncGraphDatabase.driver(uri, auth=(user, password)), **kwargs)

    async def close(self) -> None:
        await self._driver.close()

    async def ping(self) -> None:
        await self._driver.verify_connectivity()

    async def ensure_schema(self) -> None:
        for statement in STATEMENTS:
            await self._run(statement)

    async def _run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        result = await self._driver.execute_query(
            query,  # queries are module constants, never user input
            params,
            database_=self._database,
        )
        return [record.data() for record in result.records]

    # ---------- writes ----------
    async def replace_document(
        self,
        *,
        tenant_id: uuid.UUID,
        doc_id: uuid.UUID,
        title: str,
        chunks: Sequence[GraphChunk],
        extraction: GraphExtraction,
    ) -> None:
        """Idempotent: drop the document's previous subgraph, then write the new one."""
        tenant, doc = str(tenant_id), str(doc_id)
        by_index = {c.index: c for c in chunks}

        entities: dict[str, dict[str, str]] = {}
        mentions: set[tuple[str, str]] = set()
        for index, found in extraction.mentions.items():
            chunk = by_index.get(index)
            if chunk is None:
                continue
            for entity in found:
                eid = entity_id(tenant_id, entity.type, entity.name)
                entities.setdefault(
                    eid,
                    {
                        "id": eid,
                        "name": entity.name,
                        "norm": normalize(entity.name),
                        "type": entity.type,
                    },
                )
                mentions.add((chunk.chunk_id, eid))

        weights: dict[tuple[str, str, str], int] = {}
        for relation, index in extraction.relations:
            if index not in by_index:
                continue
            src = entity_id(tenant_id, relation.source.type, relation.source.name)
            dst = entity_id(tenant_id, relation.target.type, relation.target.name)
            if src in entities and dst in entities:
                key = (src, relation.type, dst)
                weights[key] = weights.get(key, 0) + 1

        params = {
            "tenant": tenant,
            "doc": doc,
            "title": title,
            "chunks": [
                {"id": c.chunk_id, "index": c.index, "page": c.page}
                for c in sorted(chunks, key=lambda c: c.index)
            ],
            "entities": list(entities.values()),
            "mentions": [{"chunk": c, "entity": e} for c, e in mentions],
            "relations": [
                {"src": s, "type": t, "dst": d, "weight": w} for (s, t, d), w in weights.items()
            ],
        }

        async def write(tx: AsyncManagedTransaction) -> None:
            await _delete_document(tx, tenant, doc)
            await tx.run(
                """
                MERGE (d:Document {id: $doc}) SET d.tenant_id = $tenant, d.title = $title
                WITH d UNWIND $chunks AS c
                MERGE (ch:Chunk {id: c.id})
                SET ch.tenant_id = $tenant, ch.doc_id = $doc, ch.index = c.index, ch.page = c.page
                MERGE (d)-[:HAS_CHUNK]->(ch)
                """,
                params,
            )
            await tx.run(
                """
                UNWIND range(0, size($chunks) - 2) AS i
                MATCH (a:Chunk {id: $chunks[i].id}), (b:Chunk {id: $chunks[i + 1].id})
                MERGE (a)-[:NEXT]->(b)
                """,
                params,
            )
            await tx.run(
                """
                UNWIND $entities AS e
                MERGE (n:Entity {id: e.id})
                ON CREATE SET n.tenant_id = $tenant, n.name = e.name,
                              n.norm = e.norm, n.type = e.type
                """,
                params,
            )
            await tx.run(
                """
                UNWIND $mentions AS m
                MATCH (c:Chunk {id: m.chunk}), (e:Entity {id: m.entity})
                MERGE (c)-[:MENTIONS]->(e)
                """,
                params,
            )
            await tx.run(
                """
                UNWIND $relations AS r
                MATCH (a:Entity {id: r.src}), (b:Entity {id: r.dst})
                MERGE (a)-[rel:RELATES {type: r.type, doc_id: $doc}]->(b)
                SET rel.weight = r.weight
                """,
                params,
            )

        async with self._driver.session(database=self._database) as session:
            await session.execute_write(write)
        logger.info(
            "document_graph_written",
            doc_id=doc,
            entities=len(entities),
            mentions=len(mentions),
            relations=len(weights),
        )

    async def delete_document(self, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        async def delete(tx: AsyncManagedTransaction) -> None:
            await _delete_document(tx, str(tenant_id), str(doc_id))

        async with self._driver.session(database=self._database) as session:
            await session.execute_write(delete)

    # ---------- reads ----------
    async def search_entities(
        self, tenant_id: uuid.UUID, text: str, *, limit: int = 20
    ) -> list[EntityHit]:
        terms = query_terms(text)
        if not terms:
            return []
        # Prefix match on each term (as-you-type search for the explorer).
        query = " OR ".join(f"{t}*" for t in terms)
        rows = await self._run(
            f"""
            CALL db.index.fulltext.queryNodes('{ENTITY_FULLTEXT_INDEX}', $q) YIELD node, score
            WHERE node.tenant_id = $tenant
            WITH node, score ORDER BY score DESC LIMIT $limit
            RETURN node.id AS id, node.name AS name, node.type AS type, score,
                   COUNT {{ (node)<-[:MENTIONS]-() }} AS mentions
            """,
            q=query,
            tenant=str(tenant_id),
            limit=limit,
        )
        return [EntityHit(**row) for row in rows]

    async def neighborhood(
        self, tenant_id: uuid.UUID, entity: str, *, hops: int = 1, limit: int = 100
    ) -> Neighborhood | None:
        hops = max(1, min(hops, 2))
        rows = await self._run(
            f"""
            MATCH (s:Entity {{id: $id, tenant_id: $tenant}})
            OPTIONAL MATCH p = (s)-[:RELATES*1..{hops}]-(:Entity)
            WITH s, p LIMIT $limit
            WITH s, collect(p) AS paths
            RETURN s {{.id, .name, .type}} AS seed,
                   [p IN paths | [n IN nodes(p) | n {{.id, .name, .type}}]] AS nodes,
                   [p IN paths | [r IN relationships(p) |
                       {{source: startNode(r).id, target: endNode(r).id, type: r.type,
                         doc_id: r.doc_id, weight: r.weight}}]] AS edges
            """,
            id=entity,
            tenant=str(tenant_id),
            limit=limit,
        )
        if not rows:
            return None
        row = rows[0]
        nodes = {row["seed"]["id"]: row["seed"]}
        for path in row["nodes"]:
            for node in path:
                nodes.setdefault(node["id"], node)
        edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for path in row["edges"]:
            for edge in path:
                edges.setdefault(
                    (edge["source"], edge["type"], edge["target"], edge["doc_id"]), edge
                )
        return Neighborhood(nodes=list(nodes.values()), edges=list(edges.values()))

    async def expand(
        self,
        tenant_id: uuid.UUID,
        question: str,
        *,
        doc_ids: Sequence[uuid.UUID] | None = None,
        hops: int = 1,
        max_seeds: int = 5,
        max_facts: int = 15,
        max_chunks: int = 30,
    ) -> Expansion:
        """Match entities named in the question, walk 1-2 hops, and return the facts
        and the chunks that mention the resulting entities (seed mentions weigh more)."""
        terms = query_terms(question)
        if not terms:
            return Expansion()
        tenant = str(tenant_id)
        docs = [str(d) for d in doc_ids or []]
        hops = max(1, min(hops, 2))

        seed_rows = await self._run(
            f"""
            CALL db.index.fulltext.queryNodes('{ENTITY_FULLTEXT_INDEX}', $q) YIELD node, score
            WHERE node.tenant_id = $tenant
            RETURN node.id AS id, node.name AS name, node.type AS type, score
            ORDER BY score DESC LIMIT $limit
            """,
            q=" OR ".join(terms),
            tenant=tenant,
            limit=max_seeds,
        )
        seeds = [EntityHit(**row) for row in seed_rows]
        if not seeds:
            return Expansion()
        seed_ids = [s.id for s in seeds]

        facts = [
            Fact(**row)
            for row in await self._run(
                """
                MATCH (a:Entity)-[r:RELATES]->(b:Entity)
                WHERE (a.id IN $seeds OR b.id IN $seeds) AND a.tenant_id = $tenant
                  AND (size($docs) = 0 OR r.doc_id IN $docs)
                RETURN a.name AS source, r.type AS relation, b.name AS target,
                       r.doc_id AS doc_id, r.weight AS weight
                ORDER BY r.weight DESC LIMIT $limit
                """,
                seeds=seed_ids,
                tenant=tenant,
                docs=docs,
                limit=max_facts,
            )
        ]

        chunk_rows = await self._run(
            f"""
            MATCH (s:Entity) WHERE s.id IN $seeds
            OPTIONAL MATCH (s)-[:RELATES*1..{hops}]-(n:Entity {{tenant_id: $tenant}})
            WITH collect(DISTINCT s.id) + collect(DISTINCT n.id) AS ids
            MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
            WHERE e.id IN ids AND c.tenant_id = $tenant AND (size($docs) = 0 OR c.doc_id IN $docs)
            WITH c, sum(CASE WHEN e.id IN $seeds THEN 2.0 ELSE 1.0 END) AS score
            RETURN c.id AS chunk_id, score ORDER BY score DESC, c.index LIMIT $limit
            """,
            seeds=seed_ids,
            tenant=tenant,
            docs=docs,
            limit=max_chunks,
        )
        return Expansion(
            seeds=seeds,
            facts=facts,
            chunks=[(row["chunk_id"], float(row["score"])) for row in chunk_rows],
        )

    async def stats(self, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> dict[str, int]:
        rows = await self._run(
            """
            MATCH (d:Document {id: $doc, tenant_id: $tenant})
            OPTIONAL MATCH (d)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(e:Entity)
            WITH d, count(DISTINCT e) AS entities
            OPTIONAL MATCH ()-[r:RELATES {doc_id: $doc}]->()
            RETURN entities, count(r) AS relations
            """,
            doc=str(doc_id),
            tenant=str(tenant_id),
        )
        return rows[0] if rows else {"entities": 0, "relations": 0}


async def _delete_document(tx: AsyncManagedTransaction, tenant: str, doc: str) -> None:
    """Remove a document's chunks and facts, then entities nothing mentions anymore."""
    result = await tx.run(
        """
        MATCH (d:Document {id: $doc, tenant_id: $tenant})
        OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
        OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity)
        WITH d, collect(DISTINCT c) AS chunks, collect(DISTINCT e.id) AS entity_ids
        FOREACH (c IN chunks | DETACH DELETE c)
        DETACH DELETE d
        RETURN entity_ids
        """,
        doc=doc,
        tenant=tenant,
    )
    record = await result.single()
    entity_ids = record["entity_ids"] if record else []
    await tx.run(
        "MATCH ()-[r:RELATES {doc_id: $doc}]->() DELETE r",
        doc=doc,
    )
    if entity_ids:
        await tx.run(
            """
            MATCH (e:Entity) WHERE e.id IN $ids AND NOT (e)<-[:MENTIONS]-()
            DETACH DELETE e
            """,
            ids=entity_ids,
        )
