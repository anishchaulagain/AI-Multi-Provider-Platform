// Generated from platform_graph.schema; applied by services on startup.
CREATE CONSTRAINT document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE;
CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE;
CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE;
CREATE INDEX document_tenant IF NOT EXISTS FOR (d:Document) ON (d.tenant_id);
CREATE INDEX chunk_tenant_doc IF NOT EXISTS FOR (c:Chunk) ON (c.tenant_id, c.doc_id);
CREATE INDEX entity_tenant_norm IF NOT EXISTS FOR (e:Entity) ON (e.tenant_id, e.norm);
CREATE FULLTEXT INDEX entity_names IF NOT EXISTS FOR (e:Entity) ON EACH [e.name];
