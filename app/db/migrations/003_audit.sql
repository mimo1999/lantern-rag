CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    roles TEXT[] NOT NULL,
    query_hash TEXT NOT NULL,
    retrieved_chunks JSONB NOT NULL,
    llm_model TEXT,
    embedding_model TEXT,
    retrieval_latency_ms DOUBLE PRECISION,
    generation_latency_ms DOUBLE PRECISION,
    authorization_decision TEXT NOT NULL,
    outcome TEXT NOT NULL
);

CREATE INDEX audit_log_tenant_time_idx ON audit_log (tenant_id, occurred_at DESC);
CREATE INDEX audit_log_user_idx ON audit_log (user_id, occurred_at DESC);

CREATE FUNCTION audit_log_reject_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_change();
