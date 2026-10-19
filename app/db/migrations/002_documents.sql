CREATE TABLE documents (
    id UUID PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('default', 'tenant')),
    tenant_id TEXT REFERENCES tenants (id),
    title TEXT NOT NULL,
    source_uri TEXT,
    checksum TEXT NOT NULL,
    allowed_roles TEXT[] NOT NULL CHECK (cardinality(allowed_roles) > 0),
    status TEXT NOT NULL DEFAULT 'indexing' CHECK (status IN ('indexing', 'ready', 'failed')),
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_id_matches_scope CHECK (
        (scope = 'default' AND tenant_id IS NULL)
        OR (scope = 'tenant' AND tenant_id IS NOT NULL)
    )
);

CREATE INDEX documents_tenant_idx ON documents (tenant_id, created_at DESC);
CREATE INDEX documents_checksum_idx ON documents (scope, tenant_id, checksum);
