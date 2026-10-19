from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import jwt
import pytest
import pytest_asyncio

from app.auth.revocation import get_token_revocations
from app.config import Settings
from app.ingestion.uploads import get_upload_tracker
from app.main import app
from app.ratelimit import get_rate_limiter
from evaluation.db import PgVectorUnavailable, isolated_pool
from evaluation.testing import VectorEnv


@pytest_asyncio.fixture
async def pg_pool() -> AsyncIterator[asyncpg.Pool]:
    """Throwaway schema with every migration; skips when Postgres or pgvector is missing."""
    try:
        async with isolated_pool() as pool:
            yield pool
    except PgVectorUnavailable as error:
        pytest.skip(f"pgvector unavailable: {error}")
    except (OSError, asyncpg.PostgresConnectionError) as error:
        pytest.skip(f"PostgreSQL unavailable: {error}")


@pytest_asyncio.fixture
async def vector_env(pg_pool: asyncpg.Pool) -> VectorEnv:
    return VectorEnv.create(pg_pool)


TEST_SETTINGS = Settings(jwt_secret_key="test-secret-that-is-at-least-thirty-two-bytes")


@pytest.fixture
def test_settings() -> Settings:
    return TEST_SETTINGS


@pytest.fixture
def token_for() -> Callable[..., str]:
    def make(user_id: str = "bob", tenant_id: str = "acme", roles: tuple[str, ...] = ("employee",)):
        return jwt.encode(
            {
                "sub": user_id,
                "tenant_id": tenant_id,
                "roles": list(roles),
                "iss": TEST_SETTINGS.jwt_issuer,
                "jti": str(uuid4()),
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
            },
            TEST_SETTINGS.jwt_secret_key,
            algorithm="HS256",
        )

    return make


class NoRevocations:
    """Default for tests that never touch the database: no token is revoked."""

    async def is_revoked(self, jti: str, user_id: str, issued_at: int) -> bool:
        return False

    async def revoke(self, jti: str, expires_at: int) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_revocation_lookup_or_rate_limit_state():
    app.dependency_overrides[get_token_revocations] = lambda: NoRevocations()
    get_rate_limiter.cache_clear()
    get_upload_tracker.cache_clear()
    yield
    app.dependency_overrides.pop(get_token_revocations, None)
