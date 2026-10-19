"""Tables and image captions live in their own table, so they need their own isolation proof."""

from uuid import NAMESPACE_URL, uuid5

import pytest
import pytest_asyncio

from app.auth.models import UserContext
from app.ingestion.indexer import Asset
from app.ingestion.registry import PostgresDocumentRegistry
from evaluation.testing import VectorEnv

ACME_FINANCE = UserContext("carol", "acme", ("finance",))
ACME_EMPLOYEE = UserContext("bob", "acme", ("employee",))
GLOBEX_FINANCE = UserContext("dave", "globex", ("finance",))

SALARY_TABLE = Asset("table", "| Role | Salary |\n| CEO | 900000 canary-acme-salary |", 1)
CHART = Asset("image", "A bar chart of revenue canary-acme-chart by quarter", 2)


@pytest_asyncio.fixture
async def env(vector_env: VectorEnv) -> VectorEnv:
    await vector_env.add(
        "acme-finance",
        "Acme finance prose.",
        ("finance",),
        tenant_id="acme",
        assets=(SALARY_TABLE, CHART),
    )
    await vector_env.add(
        "globex-finance",
        "Globex finance prose.",
        ("finance",),
        tenant_id="globex",
        assets=(Asset("table", "| Globex | secret canary-globex-table |"),),
    )
    await vector_env.add(
        "shared",
        "Shared handbook.",
        ("finance", "employee"),
        scope="default",
        assets=(Asset("table", "| Holiday | Days |\n| Leave | 25 canary-shared-table |"),),
    )
    return vector_env


async def texts(env: VectorEnv, user: UserContext, query: str) -> str:
    return " ".join(c.text for c in await env.combined.retrieve(query, user))


@pytest.mark.asyncio
async def test_assets_follow_the_same_tenant_and_role_rules_as_prose(env: VectorEnv) -> None:
    query = "salary revenue chart table holiday leave secret canary"

    finance = await texts(env, ACME_FINANCE, query)
    employee = await texts(env, ACME_EMPLOYEE, query)
    other_tenant = await texts(env, GLOBEX_FINANCE, query)

    assert "canary-acme-salary" in finance and "canary-acme-chart" in finance
    assert "canary-shared-table" in finance  # shared assets are visible to every tenant
    assert "canary-globex-table" not in finance and "canary-globex-table" in other_tenant
    assert (
        "canary-acme" not in employee and "canary-acme" not in other_tenant
    )  # wrong role / tenant
    assert "canary-shared-table" in employee and "canary-shared-table" in other_tenant


@pytest.mark.asyncio
async def test_asset_hits_are_labelled_and_limited(env: VectorEnv) -> None:
    results = await env.combined.retrieve("salary revenue chart canary", ACME_FINANCE)

    assert {c.kind for c in results} <= {"text", "table", "image"}
    assert sum(c.kind != "text" for c in results) <= 3  # asset_top_k


@pytest.mark.asyncio
async def test_listing_includes_documents_that_only_have_assets(vector_env: VectorEnv) -> None:
    await vector_env.add(
        "tables-only",
        "",
        ("finance",),
        tenant_id="acme",
        title="Tables Only",
        assets=(SALARY_TABLE,),
    )

    finance = await vector_env.combined.list_documents(ACME_FINANCE)
    employee = await vector_env.combined.list_documents(ACME_EMPLOYEE)

    assert [d.title for d in finance] == ["Tables Only"]
    assert employee == []


@pytest.mark.asyncio
async def test_deleting_a_document_removes_its_asset_vectors(env: VectorEnv) -> None:
    registry = PostgresDocumentRegistry(env.pool)
    before = await env.pool.fetchval("SELECT count(*) FROM asset_chunks WHERE tenant_id = 'acme'")

    assert await registry.delete(
        str(uuid5(NAMESPACE_URL, "acme-finance")), "acme", include_default=False
    )

    assert before == 2
    assert (
        await env.pool.fetchval("SELECT count(*) FROM asset_chunks WHERE tenant_id = 'acme'") == 0
    )
    assert await env.pool.fetchval("SELECT count(*) FROM asset_chunks") == 2  # globex + shared
