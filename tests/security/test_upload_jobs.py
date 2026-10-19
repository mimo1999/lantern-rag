"""Background uploads against real Postgres: status, isolation, duplicates, failures, staleness."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.ingestion.dependencies import get_document_registry, get_ingestion_service
from app.ingestion.registry import STALE_ERROR, PostgresDocumentRegistry
from app.ingestion.service import IngestionService
from app.main import app
from evaluation.testing import VectorEnv


@pytest_asyncio.fixture
async def env(vector_env: VectorEnv, test_settings):
    registry = PostgresDocumentRegistry(vector_env.pool)
    service = IngestionService(registry, vector_env.indexer)  # no captioner: text files only
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_ingestion_service] = lambda: service
    app.dependency_overrides[get_document_registry] = lambda: registry
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    yield client, registry, vector_env.pool
    app.dependency_overrides.clear()


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def upload(client, token, name="notes.md", body=b"# Notes\nQuarterly revenue", roles="admin"):
    return await client.post(
        "/documents/upload",
        files={"file": (name, body, "text/markdown")},
        data={"allowed_roles": roles},
        headers=headers(token),
    )


async def status_of(client, token, document_id):
    return await client.get(f"/documents/{document_id}/status", headers=headers(token))


@pytest.mark.asyncio
async def test_upload_is_accepted_then_reported_ready(env, token_for, vector_env) -> None:
    client, _, pool = env
    alice = token_for("alice", "acme", ("admin",))
    await pool.execute(
        "INSERT INTO tenants (id, name) VALUES ('acme', 'acme') ON CONFLICT DO NOTHING"
    )

    accepted = await upload(client, alice)
    document_id = accepted.json()["document_id"]
    done = (await status_of(client, alice, document_id)).json()

    assert accepted.status_code == 202 and accepted.json()["status"] == "processing"
    assert done["status"] == "ready" and done["chunks_indexed"] >= 1 and done["error"] is None
    stored = await pool.fetchrow(
        "SELECT tenant_id, scope FROM documents WHERE id = $1::uuid", document_id
    )
    assert (stored["tenant_id"], stored["scope"]) == ("acme", "tenant")


@pytest.mark.asyncio
async def test_identical_upload_is_reported_as_a_duplicate_of_the_first(env, token_for) -> None:
    client, _, pool = env
    alice = token_for("alice", "acme", ("admin",))
    await pool.execute(
        "INSERT INTO tenants (id, name) VALUES ('acme', 'acme') ON CONFLICT DO NOTHING"
    )

    first = (await upload(client, alice)).json()["document_id"]
    second = (await upload(client, alice)).json()["document_id"]
    result = (await status_of(client, alice, second)).json()

    assert first != second
    assert result["status"] == "duplicate" and result["duplicate_of"] == first
    chunks = await pool.fetchval(
        "SELECT count(*) FROM document_chunks WHERE document_id = $1::uuid", second
    )
    assert chunks == 0  # nothing was indexed twice


@pytest.mark.asyncio
async def test_an_unreadable_file_fails_with_a_message_and_is_listed_until_deleted(
    env, token_for
) -> None:
    client, _, pool = env
    alice = token_for("alice", "acme", ("admin",))
    await pool.execute(
        "INSERT INTO tenants (id, name) VALUES ('acme', 'acme') ON CONFLICT DO NOTHING"
    )

    document_id = (await upload(client, alice, "scan.pdf", b"not a pdf")).json()["document_id"]
    failed = (await status_of(client, alice, document_id)).json()
    listed = (await client.get("/documents/uploads", headers=headers(alice))).json()
    deleted = await client.delete(f"/documents/{document_id}", headers=headers(alice))
    after = (await client.get("/documents/uploads", headers=headers(alice))).json()

    assert failed["status"] == "failed" and "not a valid PDF" in failed["error"]
    assert [row["document_id"] for row in listed] == [document_id]
    assert deleted.status_code == 204 and after == []


@pytest.mark.asyncio
async def test_status_and_listing_never_cross_tenants_or_roles(env, token_for) -> None:
    client, _, pool = env
    await pool.execute(
        "INSERT INTO tenants (id, name) VALUES ('acme', 'acme'), ('globex', 'globex')"
        " ON CONFLICT DO NOTHING"
    )
    alice = token_for("alice", "acme", ("admin",))
    document_id = (await upload(client, alice, "bad.pdf", b"not a pdf")).json()["document_id"]

    other_tenant_admin = token_for("erin", "globex", ("admin",))
    employee = token_for("bob", "acme", ("employee",))

    assert (await status_of(client, other_tenant_admin, document_id)).status_code == 404
    assert (
        await client.get("/documents/uploads", headers=headers(other_tenant_admin))
    ).json() == []
    assert (await status_of(client, employee, document_id)).status_code == 403
    assert (await client.get("/documents/uploads", headers=headers(employee))).status_code == 403


@pytest.mark.asyncio
async def test_shared_uploads_are_visible_only_to_platform_admin(env, token_for) -> None:
    client, _, _ = env
    root = token_for("root", "platform", ("platform-admin", "admin"))
    tenant_admin = token_for("alice", "acme", ("admin",))
    accepted = await client.post(
        "/documents/default/upload",
        files={"file": ("handbook.md", b"# Handbook\nwelcome", "text/markdown")},
        data={"allowed_roles": "employee"},
        headers=headers(root),
    )
    document_id = accepted.json()["document_id"]

    assert (await status_of(client, root, document_id)).json()["status"] == "ready"
    assert (await status_of(client, tenant_admin, document_id)).status_code == 404


@pytest.mark.asyncio
async def test_abandoned_jobs_are_reported_failed_instead_of_processing_forever(
    env, token_for
) -> None:
    client, registry, pool = env
    alice = token_for("alice", "acme", ("admin",))
    await pool.execute(
        "INSERT INTO tenants (id, name) VALUES ('acme', 'acme') ON CONFLICT DO NOTHING"
    )
    fresh = await IngestionService(registry, None).start(  # type: ignore[arg-type]
        scope="tenant",
        tenant_id="acme",
        title="Fresh",
        source_uri="a.md",
        allowed_roles=("admin",),
        created_by="alice",
    )
    stale = await IngestionService(registry, None).start(  # type: ignore[arg-type]
        scope="tenant",
        tenant_id="acme",
        title="Stale",
        source_uri="b.md",
        allowed_roles=("admin",),
        created_by="alice",
    )
    await pool.execute(
        "UPDATE documents SET created_at = now() - interval '31 minutes' WHERE id = $1::uuid", stale
    )

    assert (await status_of(client, alice, fresh)).json()["status"] == "processing"
    abandoned = (await status_of(client, alice, stale)).json()
    assert abandoned["status"] == "failed" and abandoned["error"] == STALE_ERROR


@pytest.mark.asyncio
async def test_a_finished_document_cannot_be_overwritten_by_a_late_failure(env, token_for) -> None:
    client, registry, pool = env
    alice = token_for("alice", "acme", ("admin",))
    await pool.execute(
        "INSERT INTO tenants (id, name) VALUES ('acme', 'acme') ON CONFLICT DO NOTHING"
    )
    document_id = (await upload(client, alice)).json()["document_id"]

    await registry.mark_failed(document_id, "late failure")

    assert (await status_of(client, alice, document_id)).json()["status"] == "ready"
