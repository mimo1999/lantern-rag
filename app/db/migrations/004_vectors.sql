-- Requires the pgvector extension. Creating it needs sufficient privilege (pgvector is not a
-- "trusted" extension), so run `CREATE EXTENSION vector;` once as a superuser if this fails.
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per chunk. scope / tenant_id / allowed_roles are copied from the document so the
-- authorization filter and the similarity search read a single table (index-friendly).
-- The embedding dimension is fixed by the column type: changing embedding models to a
-- different dimension requires a new migration and re-embedding every chunk.
CREATE TABLE document_chunks (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL CHECK (scope IN ('default', 'tenant')),
    tenant_id TEXT REFERENCES tenants (id),
    allowed_roles TEXT[] NOT NULL CHECK (cardinality(allowed_roles) > 0),
    title TEXT NOT NULL,
    source_uri TEXT,
    content TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding vector(384) NOT NULL,
    CONSTRAINT chunk_tenant_matches_scope CHECK (
        (scope = 'default' AND tenant_id IS NULL)
        OR (scope = 'tenant' AND tenant_id IS NOT NULL)
    )
);

CREATE INDEX document_chunks_document_idx ON document_chunks (document_id);
CREATE INDEX document_chunks_tenant_idx ON document_chunks (tenant_id);
CREATE INDEX document_chunks_roles_idx ON document_chunks USING gin (allowed_roles);
CREATE INDEX document_chunks_embedding_idx
    ON document_chunks USING hnsw (embedding vector_cosine_ops);
