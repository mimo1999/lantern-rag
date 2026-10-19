import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from app.audit.dependencies import get_audit_repository, get_audit_service
from app.audit.models import AuditEntry, AuditFilter
from app.audit.repository import PostgresAuditRepository
from app.audit.service import AuditService, hash_query
from app.auth.models import UserContext
from app.config import get_settings
from app.generation.dependencies import get_answer_service
from app.generation.service import AnswerResult
from app.main import app
from app.retrieval.service import RetrievedChunk

REQUEST_A = "11111111-1111-4111-8111-111111111111"
REQUEST_B = "22222222-2222-4222-8222-222222222222"


def entry(
    request_id: str, tenant: str, user: str, document_ids: tuple[str, ...] = ()
) -> AuditEntry:
    return AuditEntry(
        request_id=request_id,
        user_id=user,
        tenant_id=tenant,
        roles=("employee",),
        query_hash=hash_query("q"),
        retrieved_chunks=[{"document_id": d, "chunk_id": f"{d}_0001"} for d in document_ids],
        outcome="answered",
    )


@pytest.mark.asyncio
async def test_repository_round_trips_and_filters(pg_pool: asyncpg.Pool) -> None:
    repo = PostgresAuditRepository(pg_pool)
    await repo.append(entry(REQUEST_A, "acme", "bob", ("doc-1",)))
    await repo.append(entry(REQUEST_B, "globex", "dave", ("doc-2",)))

    assert {r.entry.request_id for r in await repo.search(AuditFilter())} == {REQUEST_A, REQUEST_B}
    acme = await repo.search(AuditFilter(tenant_id="acme"))
    assert [r.entry.request_id for r in acme] == [REQUEST_A]
    assert acme[0].entry.retrieved_chunks == [{"document_id": "doc-1", "chunk_id": "doc-1_0001"}]
    assert [r.entry.user_id for r in await repo.search(AuditFilter(user_id="dave"))] == ["dave"]
    by_doc = await repo.search(AuditFilter(document_id="doc-2"))
    assert [r.entry.request_id for r in by_doc] == [REQUEST_B]


@pytest.fixture
def api(pg_pool: asyncpg.Pool, test_settings):
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_audit_repository] = lambda: PostgresAuditRepository(pg_pool)
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_tenant_admin_only_sees_own_tenant_even_when_requesting_another(
    api, pg_pool: asyncpg.Pool, token_for
) -> None:
    repo = PostgresAuditRepository(pg_pool)
    await repo.append(entry(REQUEST_A, "acme", "bob"))
    await repo.append(entry(REQUEST_B, "globex", "dave"))
    headers = {"Authorization": f"Bearer {token_for('alice', 'acme', ('admin',))}"}

    async with api as client:
        own = await client.get("/audit", headers=headers)
        spoofed = await client.get("/audit", params={"tenant_id": "globex"}, headers=headers)

    assert [r["request_id"] for r in own.json()] == [REQUEST_A]
    assert [r["request_id"] for r in spoofed.json()] == [REQUEST_A]


@pytest.mark.asyncio
async def test_platform_admin_can_query_across_tenants(
    api, pg_pool: asyncpg.Pool, token_for
) -> None:
    repo = PostgresAuditRepository(pg_pool)
    await repo.append(entry(REQUEST_A, "acme", "bob"))
    await repo.append(entry(REQUEST_B, "globex", "dave"))
    token = token_for("root", "platform", ("platform-admin", "admin"))
    headers = {"Authorization": f"Bearer {token}"}

    async with api as client:
        everything = await client.get("/audit", headers=headers)
        globex = await client.get("/audit", params={"tenant_id": "globex"}, headers=headers)

    assert {r["request_id"] for r in everything.json()} == {REQUEST_A, REQUEST_B}
    assert [r["request_id"] for r in globex.json()] == [REQUEST_B]


@pytest.mark.asyncio
async def test_non_admin_cannot_read_audit(api, token_for) -> None:
    async with api as client:
        response = await client.get(
            "/audit", headers={"Authorization": f"Bearer {token_for('bob', 'acme', ('employee',))}"}
        )
    assert response.status_code == 403


class FixedAnswers:
    def __init__(self, result: AnswerResult) -> None:
        self._result = result

    async def answer(self, question: str, user: UserContext) -> AnswerResult:
        return self._result


def answered() -> AnswerResult:
    chunk = RetrievedChunk("doc-1", "doc-1_0001", "Handbook", "default", "text", 0.9)
    return AnswerResult("the answer", [chunk], "answered", "fake-llm", 5, 3, 12.0, 30.0)


@pytest.mark.asyncio
async def test_query_writes_audit_record_matching_returned_sources(
    pg_pool: asyncpg.Pool, test_settings, token_for
) -> None:
    repo = PostgresAuditRepository(pg_pool)
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_answer_service] = lambda: FixedAnswers(answered())
    app.dependency_overrides[get_audit_service] = lambda: AuditService(repo, "fake-embed")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/query",
                json={"query": "What is in the handbook?"},
                headers={"Authorization": f"Bearer {token_for('bob', 'acme', ('employee',))}"},
            )
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    [record] = await repo.search(AuditFilter())
    assert response.status_code == 200
    assert record.entry.request_id == body["request_id"]
    assert (record.entry.user_id, record.entry.tenant_id) == ("bob", "acme")
    assert [c["chunk_id"] for c in record.entry.retrieved_chunks] == [
        s["chunk_id"] for s in body["sources"]
    ]
    assert record.entry.query_hash == hash_query("What is in the handbook?")
    assert record.entry.embedding_model == "fake-embed" and record.entry.llm_model == "fake-llm"


class BrokenAuditRepo:
    async def append(self, entry: AuditEntry) -> None:
        raise ConnectionError("db down")

    async def search(self, criteria: AuditFilter):
        return []


@pytest.mark.asyncio
async def test_query_fails_closed_when_audit_cannot_be_written(test_settings, token_for) -> None:
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_answer_service] = lambda: FixedAnswers(answered())
    app.dependency_overrides[get_audit_service] = lambda: AuditService(BrokenAuditRepo(), "e")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/query",
                json={"query": "q"},
                headers={"Authorization": f"Bearer {token_for()}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert "the answer" not in response.text
