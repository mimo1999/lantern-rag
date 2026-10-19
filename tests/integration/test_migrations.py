import asyncpg
import pytest


@pytest.mark.asyncio
async def test_migrations_create_expected_tables(pg_pool: asyncpg.Pool) -> None:
    rows = await pg_pool.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )
    assert {row["table_name"] for row in rows} >= {
        "tenants",
        "users",
        "user_roles",
        "documents",
        "audit_log",
        "schema_migrations",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "tenant_id"),
    [("default", "acme"), ("tenant", None)],
)
async def test_documents_scope_constraint_rejects_inconsistent_rows(
    pg_pool: asyncpg.Pool, scope: str, tenant_id: str | None
) -> None:
    await pg_pool.execute("INSERT INTO tenants (id, name) VALUES ('acme', 'Acme')")
    with pytest.raises(asyncpg.CheckViolationError):
        await pg_pool.execute(
            "INSERT INTO documents (id, scope, tenant_id, title, checksum, allowed_roles,"
            " created_by) VALUES (gen_random_uuid(), $1, $2, 't', 'c', ARRAY['admin'], 'u')",
            scope,
            tenant_id,
        )


@pytest.mark.asyncio
async def test_audit_log_is_append_only(pg_pool: asyncpg.Pool) -> None:
    await pg_pool.execute(
        "INSERT INTO audit_log (request_id, user_id, tenant_id, roles, query_hash,"
        " retrieved_chunks, authorization_decision, outcome)"
        " VALUES (gen_random_uuid(), 'u', 't', ARRAY['employee'], 'h', '[]'::jsonb, 'allow', 'ok')"
    )
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await pg_pool.execute("UPDATE audit_log SET outcome = 'tampered'")
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await pg_pool.execute("DELETE FROM audit_log")
