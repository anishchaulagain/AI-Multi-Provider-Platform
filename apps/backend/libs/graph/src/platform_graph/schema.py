"""Graph model, fixed extraction schema and Neo4j constraints/indexes.

    (:Document {id, tenant_id, title})-[:HAS_CHUNK]->(:Chunk {id, tenant_id, doc_id, index, page})
    (:Chunk)-[:NEXT]->(:Chunk)
    (:Chunk)-[:MENTIONS]->(:Entity {id, tenant_id, name, norm, type})
    (:Entity)-[:RELATES {type, doc_id, weight}]->(:Entity)

RELATES edges carry the document they came from, so deleting or re-ingesting a
document removes exactly its facts. Every query filters by tenant_id.

`python -m platform_graph.schema > infra/neo4j/init.cypher` regenerates the
reference file; services apply the same statements on startup.
"""

import re
import uuid

ENTITY_TYPES = ("Person", "Organization", "Product", "Technology", "Concept", "Location", "Event")
DEFAULT_ENTITY_TYPE = "Concept"

RELATION_TYPES = (
    "PART_OF",
    "WORKS_FOR",
    "LOCATED_IN",
    "CREATED_BY",
    "USES",
    "DEPENDS_ON",
    "COMPETES_WITH",
    "OCCURRED_AT",
    "RELATED_TO",
)
DEFAULT_RELATION_TYPE = "RELATED_TO"

ENTITY_FULLTEXT_INDEX = "entity_names"

STATEMENTS = (
    "CREATE CONSTRAINT document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE",
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
    "CREATE INDEX document_tenant IF NOT EXISTS FOR (d:Document) ON (d.tenant_id)",
    "CREATE INDEX chunk_tenant_doc IF NOT EXISTS FOR (c:Chunk) ON (c.tenant_id, c.doc_id)",
    "CREATE INDEX entity_tenant_norm IF NOT EXISTS FOR (e:Entity) ON (e.tenant_id, e.norm)",
    f"CREATE FULLTEXT INDEX {ENTITY_FULLTEXT_INDEX} IF NOT EXISTS FOR (e:Entity) ON EACH [e.name]",
)

_ENTITY_NS = uuid.UUID("5b0f4a3e-6a43-4d4b-9a51-2f0f6c2b8d11")


def normalize(name: str) -> str:
    """Key for entity resolution: case, punctuation, whitespace and a leading
    article don't make two entities different."""
    text = re.sub(r"[^\w\s]", " ", name.lower())
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^(the|a|an) ", "", text)


def entity_id(tenant_id: uuid.UUID | str, entity_type: str, name: str) -> str:
    return str(uuid.uuid5(_ENTITY_NS, f"{tenant_id}:{entity_type}:{normalize(name)}"))


def canonical_entity_type(value: str) -> str:
    lookup = {t.lower(): t for t in ENTITY_TYPES}
    aliases = {"org": "Organization", "company": "Organization", "place": "Location"}
    key = value.strip().lower()
    return lookup.get(key) or aliases.get(key) or DEFAULT_ENTITY_TYPE


def canonical_relation_type(value: str) -> str:
    key = re.sub(r"[^A-Z]+", "_", value.strip().upper()).strip("_")
    return key if key in RELATION_TYPES else DEFAULT_RELATION_TYPE


if __name__ == "__main__":
    print("// Generated from platform_graph.schema; applied by services on startup.")
    for statement in STATEMENTS:
        print(f"{statement};")
