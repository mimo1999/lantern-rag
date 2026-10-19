-- Uploads are indexed in the background, so the registry row now records progress and outcome.
-- 'duplicate' means identical content (and roles) was already indexed; `duplicate_of` names it.
ALTER TABLE documents DROP CONSTRAINT documents_status_check;
ALTER TABLE documents
    ADD CONSTRAINT documents_status_check
        CHECK (status IN ('indexing', 'ready', 'failed', 'duplicate')),
    -- A message safe to show the uploader; never an exception text or a stack trace.
    ADD COLUMN error TEXT,
    ADD COLUMN duplicate_of UUID,
    ADD COLUMN tables_indexed INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN images_indexed INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN images_skipped INTEGER NOT NULL DEFAULT 0;

CREATE INDEX documents_unfinished_idx ON documents (tenant_id, created_at DESC)
    WHERE status <> 'ready';
