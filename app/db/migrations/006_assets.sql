-- Tables and images from PDFs are embedded with a different model than prose, so they live in
-- their own table: a vector column has one dimension, and vectors from different embedding
-- models must never share a column. Same authorization columns as document_chunks, so the
-- same filter applies unchanged.
ALTER TABLE document_chunks
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'text' CHECK (kind = 'text'),
    ADD COLUMN page INTEGER;

-- `content` is a table as markdown, or the vision model's caption of an image.
-- Changing the asset embedding model to a different dimension needs a new migration and
-- re-ingesting the affected documents.
CREATE TABLE asset_chunks (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('table', 'image')),
    page INTEGER,
    scope TEXT NOT NULL CHECK (scope IN ('default', 'tenant')),
    tenant_id TEXT REFERENCES tenants (id),
    allowed_roles TEXT[] NOT NULL CHECK (cardinality(allowed_roles) > 0),
    title TEXT NOT NULL,
    source_uri TEXT,
    content TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    CONSTRAINT asset_tenant_matches_scope CHECK (
        (scope = 'default' AND tenant_id IS NULL)
        OR (scope = 'tenant' AND tenant_id IS NOT NULL)
    )
);

CREATE INDEX asset_chunks_document_idx ON asset_chunks (document_id);
CREATE INDEX asset_chunks_tenant_idx ON asset_chunks (tenant_id);
CREATE INDEX asset_chunks_roles_idx ON asset_chunks USING gin (allowed_roles);
CREATE INDEX asset_chunks_embedding_idx ON asset_chunks USING hnsw (embedding vector_cosine_ops);
