import pytest

from app.auth.models import UserContext
from evaluation.testing import VectorEnv


@pytest.mark.asyncio
async def test_catalog_lists_default_and_own_tenant_documents_but_not_other_tenants(
    vector_env: VectorEnv,
) -> None:
    await vector_env.add(
        "handbook",
        "Applies to everyone in every tenant regardless of role.",
        ("admin", "employee", "finance"),
        scope="default",
        title="Employee Handbook",
    )
    await vector_env.add(
        "acme-finance",
        "Acme secret revenue is 81.4 million euros. " * 60,  # several chunks, one listing
        ("admin", "finance"),
        tenant_id="acme",
        title="Acme Finance",
    )
    await vector_env.add(
        "globex-finance",
        "Globex secret revenue is 112.7 million euros.",
        ("admin", "finance"),
        tenant_id="globex",
        title="Globex Finance",
    )
    catalog = vector_env.service

    finance = await catalog.list_documents(UserContext("alice", "acme", ("finance",)))
    employee = await catalog.list_documents(UserContext("bob", "acme", ("employee",)))

    assert [doc.title for doc in finance] == ["Acme Finance", "Employee Handbook"]
    assert [doc.title for doc in employee] == ["Employee Handbook"]
    assert {doc.scope for doc in finance} == {"tenant", "default"}
