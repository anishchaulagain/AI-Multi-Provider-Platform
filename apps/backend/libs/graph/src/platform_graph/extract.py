"""Entity/relation extraction with a fixed schema, via the gateway's `extract` alias.

Chunks are sent in batches (several per call) to keep the call count low on
free-tier models; the model answers in JSON validated against `BatchExtraction`.
Output is cleaned: types are coerced into the schema, relations must connect
entities found in the same chunk, and duplicates are merged.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

import structlog
from pydantic import BaseModel, Field, ValidationError

from llm_client import Caller, ChatMessage, GatewayClient, GatewayError
from platform_graph.schema import (
    ENTITY_TYPES,
    RELATION_TYPES,
    canonical_entity_type,
    canonical_relation_type,
    normalize,
)

logger = structlog.get_logger(__name__)

_MAX_NAME = 120


class ExtractedEntity(BaseModel):
    name: str
    type: str


class ExtractedRelation(BaseModel):
    source: str
    type: str
    target: str


class ChunkExtraction(BaseModel):
    chunk: int = Field(description="The chunk number given in [chunk N]")
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)


class BatchExtraction(BaseModel):
    chunks: list[ChunkExtraction] = Field(default_factory=list)


@dataclass(frozen=True)
class Entity:
    name: str
    type: str

    @property
    def key(self) -> tuple[str, str]:
        return self.type, normalize(self.name)


@dataclass(frozen=True)
class Relation:
    source: Entity
    type: str
    target: Entity


@dataclass
class GraphExtraction:
    # chunk index -> entities mentioned in it
    mentions: dict[int, list[Entity]] = field(default_factory=dict)
    # (relation, chunk index) pairs; the same fact in two chunks counts twice
    relations: list[tuple[Relation, int]] = field(default_factory=list)
    failed_batches: int = 0

    @property
    def entity_count(self) -> int:
        return len({e.key for ents in self.mentions.values() for e in ents})


_SYSTEM = (
    "You extract a knowledge graph from document chunks.\n"
    f"Entity types (use exactly one): {', '.join(ENTITY_TYPES)}.\n"
    f"Relation types (use exactly one): {', '.join(RELATION_TYPES)}.\n"
    "Rules: only extract entities explicitly named in the chunk (proper names, products, "
    "technologies, key concepts); skip generic words. Use the name as written. Relations "
    "must connect two entities from the same chunk; source and target are entity names. "
    "Return every chunk number, with empty lists when nothing qualifies."
)


def _batches(chunks: Sequence[tuple[int, str]], max_chars: int) -> list[list[tuple[int, str]]]:
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    size = 0
    for index, text in chunks:
        if current and size + len(text) > max_chars:
            batches.append(current)
            current, size = [], 0
        current.append((index, text[:max_chars]))
        size += len(text)
    if current:
        batches.append(current)
    return batches


def _clean(raw: ChunkExtraction) -> tuple[list[Entity], list[Relation]]:
    entities: dict[tuple[str, str], Entity] = {}
    by_name: dict[str, Entity] = {}
    for item in raw.entities:
        name = " ".join(item.name.split())[:_MAX_NAME]
        if len(normalize(name)) < 2:
            continue
        entity = Entity(name=name, type=canonical_entity_type(item.type))
        entities.setdefault(entity.key, entity)
        by_name.setdefault(normalize(name), entities[entity.key])

    relations: dict[tuple[tuple[str, str], str, tuple[str, str]], Relation] = {}
    for rel in raw.relations:
        source, target = by_name.get(normalize(rel.source)), by_name.get(normalize(rel.target))
        if source is None or target is None or source.key == target.key:
            continue
        relation = Relation(source, canonical_relation_type(rel.type), target)
        relations.setdefault((source.key, relation.type, target.key), relation)
    return list(entities.values()), list(relations.values())


async def extract_graph(
    gateway: GatewayClient,
    caller: Caller,
    chunks: Sequence[tuple[int, str]],
    *,
    model: str = "extract",
    batch_chars: int = 6000,
) -> GraphExtraction:
    """`chunks` are (chunk_index, text). A failed batch is skipped, not fatal."""
    result = GraphExtraction()
    for batch in _batches(chunks, batch_chars):
        wanted = {index for index, _ in batch}
        body = "\n\n".join(f"[chunk {index}]\n{text}" for index, text in batch)
        try:
            answer = await gateway.judge(
                [
                    ChatMessage(role="system", content=_SYSTEM),
                    ChatMessage(role="user", content=body),
                ],
                BatchExtraction,
                caller=caller,
                model=model,
            )
        except (GatewayError, ValidationError, ValueError) as exc:
            result.failed_batches += 1
            logger.warning(
                "graph_extraction_batch_failed",
                chunks=sorted(wanted),
                error=repr(exc)[:300],
            )
            continue
        for item in answer.data.chunks:
            if item.chunk not in wanted:
                continue
            entities, relations = _clean(item)
            if entities:
                result.mentions.setdefault(item.chunk, []).extend(entities)
            result.relations.extend((r, item.chunk) for r in relations)
    return result
