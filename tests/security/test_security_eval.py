from dataclasses import dataclass

import asyncpg
import pytest

from app.auth.models import UserContext
from app.retrieval import service as retrieval_service
from evaluation.security_eval import run


@pytest.mark.asyncio
async def test_adversarial_suite_finds_zero_leakage_and_full_authorized_recall(
    pg_pool: asyncpg.Pool,
) -> None:
    report = await run(pg_pool)

    assert report.adversarial_queries > 100
    assert report.unauthorized_retrieval_rate == 0.0
    assert report.cross_tenant_leakage_rate == 0.0
    assert report.unauthorized_answer_rate == 0.0
    assert report.authorized_recall == 1.0, report.failures


@dataclass(frozen=True)
class RoleOnlyFilter:
    """A sabotaged filter: tenant isolation removed, only role intersection remains."""

    roles: tuple[str, ...]

    def to_sql(self, first_param: int) -> tuple[str, list[object]]:
        return f"allowed_roles && ${first_param}::text[]", [list(self.roles)]


@pytest.mark.asyncio
async def test_harness_detects_leakage_when_tenant_isolation_is_removed(
    pg_pool: asyncpg.Pool, monkeypatch
) -> None:
    def role_only_filter(user: UserContext) -> RoleOnlyFilter:
        return RoleOnlyFilter(user.roles)

    monkeypatch.setattr(retrieval_service, "build_authorization_filter", role_only_filter)

    report = await run(pg_pool)

    assert report.cross_tenant_leakage_rate > 0
    assert report.unauthorized_answer_rate > 0
    assert report.failures
