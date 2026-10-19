-- Account lockout, password reset by email, and token revocation.
ALTER TABLE users
    ADD COLUMN email TEXT,
    ADD COLUMN failed_logins INTEGER NOT NULL DEFAULT 0,
    -- Set after too many failed logins; only a completed password reset clears it.
    ADD COLUMN locked_at TIMESTAMPTZ,
    -- Tokens issued before this instant are rejected (set when the password is reset).
    ADD COLUMN tokens_valid_after TIMESTAMPTZ;

CREATE UNIQUE INDEX users_email_idx ON users (tenant_id, lower(email)) WHERE email IS NOT NULL;

-- Only a SHA-256 of the emailed token is stored, so a database read cannot reset accounts.
CREATE TABLE password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX password_resets_user_idx ON password_resets (user_id, created_at DESC);

-- Denylist of signed-out tokens by `jti`; a row is only needed until the token would expire.
CREATE TABLE revoked_tokens (
    jti TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL
);
