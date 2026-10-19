from uuid import uuid4

import asyncpg
import pytest

from app.auth.models import UserContext
from app.retrieval.embeddings import HashingEmbedder
from app.retrieval.filters import AuthorizationFilter, build_authorization_filter
from app.retrieval.pgvector import ChunkRecord, PgVectorRetriever, VectorStoreConfigError
from evaluation.corpus import EVAL_DIMENSION
from evaluation.testing import VectorEnv


def user(name: str, tenant: str, *roles: str) -> UserContext:
    return UserContext(user_id=name, tenant_id=tenant, roles=roles)


def auth_filter(tenant: str, *roles: str) -> AuthorizationFilter:
    return build_authorization_filter(UserContext("u", tenant, roles))


@pytest.mark.asyncio
async def test_search_excludes_other_tenants_and_roles(vector_env: VectorEnv) -> None:
    await vector_env.add(
        "acme-finance", "Acme secret revenue is 81.4 million euros.", ("finance",), tenant_id="acme"
    )
    ask = vector_env.service.retrieve

    finance = await ask("What is the revenue?", user("a", "acme", "finance"))
    employee = await ask("What is the revenue?", user("b", "acme", "employee"))
    foreign = await ask("What is the revenue?", user("c", "globex", "finance"))

    assert vector_env.names(finance) == ["acme-finance"]
    assert employee == [] and foreign == []


@pytest.mark.asyncio
async def test_default_scope_reachable_by_every_tenant_but_tenant_docs_stay_isolated(
    vector_env: VectorEnv,
) -> None:
    await vector_env.add(
        "handbook",
        "This handbook applies to every tenant and every employee.",
        ("admin", "employee"),
        scope="default",
    )
    await vector_env.add(
        "globex-finance",
        "Globex secret revenue is 112.7 million euros.",
        ("admin", "employee"),
        tenant_id="globex",
    )

    acme = await vector_env.service.retrieve("handbook policy", user("b", "acme", "employee"))
    globex = await vector_env.service.retrieve("handbook policy", user("d", "globex", "employee"))

    assert set(vector_env.names(acme)) == {"handbook"}
    assert set(vector_env.names(globex)) == {"handbook", "globex-finance"}


@pytest.mark.asyncio
async def test_hostile_identity_values_cannot_inject_sql_or_widen_access(
    vector_env: VectorEnv,
) -> None:
    await vector_env.add("acme-doc", "Acme internal notes.", ("employee",), tenant_id="acme")

    results = await vector_env.service.retrieve(
        "notes", user("x", "acme' OR '1'='1", "employee' OR '1'='1")
    )

    assert results == []
    assert await vector_env.pool.fetchval("SELECT count(*) FROM document_chunks") == 1


@pytest.mark.asyncio
async def test_chunks_from_a_different_embedding_model_are_never_returned(
    vector_env: VectorEnv,
) -> None:
    await vector_env.add("acme-doc", "Acme vacation policy.", ("employee",), tenant_id="acme")
    other = HashingEmbedder(EVAL_DIMENSION)
    other.model = "some-other-model"  # same dimension, different model
    other_retriever = PgVectorRetriever(vector_env.pool, other)
    allowed = auth_filter("acme", "employee")

    same_model = await vector_env.retriever.search("vacation", allowed, limit=5)
    other_model = await other_retriever.search("vacation", allowed, limit=5)

    assert len(same_model) == 1 and other_model == []


@pytest.mark.asyncio
async def test_dimension_mismatch_is_reported_before_any_query(pg_pool: asyncpg.Pool) -> None:
    retriever = PgVectorRetriever(pg_pool, HashingEmbedder(512))

    with pytest.raises(VectorStoreConfigError, match="384"):
        await retriever.search("q", auth_filter("acme", "employee"), limit=1)


@pytest.mark.asyncio
async def test_a_failing_chunk_rolls_back_the_whole_document(vector_env: VectorEnv) -> None:
    await vector_env.pool.execute("INSERT INTO tenants (id, name) VALUES ('acme', 'Acme')")
    document_id = "11111111-1111-4111-8111-111111111111"
    await vector_env.pool.execute(
        "INSERT INTO documents (id, scope, tenant_id, title, checksum, allowed_roles, created_by)"
        " VALUES ($1::uuid, 'tenant', 'acme', 't', 'c', ARRAY['employee'], 'u')",
        document_id,
    )

    def chunk(chunk_id: str, doc: str) -> ChunkRecord:
        return ChunkRecord(
            id=str(uuid4()),
            document_id=doc,
            chunk_id=chunk_id,
            scope="tenant",
            tenant_id="acme",
            allowed_roles=("employee",),
            title="t",
            source_uri=None,
            content="text",
            embedding=[0.1] * EVAL_DIMENSION,
        )

    missing_document = "22222222-2222-4222-8222-222222222222"
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await vector_env.retriever.upsert_chunks(
            [chunk("ok_0000", document_id), chunk("bad_0001", missing_document)]
        )

    assert await vector_env.pool.fetchval("SELECT count(*) FROM document_chunks") == 0


@pytest.mark.asyncio
async def test_deleting_a_document_removes_its_chunks(vector_env: VectorEnv) -> None:
    await vector_env.add("acme-doc", "Acme vacation policy.", ("employee",), tenant_id="acme")
    assert await vector_env.pool.fetchval("SELECT count(*) FROM document_chunks") == 1

    await vector_env.pool.execute("DELETE FROM documents")

    assert await vector_env.pool.fetchval("SELECT count(*) FROM document_chunks") == 0
    assert await vector_env.service.retrieve("vacation", user("b", "acme", "employee")) == []
