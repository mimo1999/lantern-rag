from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.dependencies import get_retrieval_service
from app.main import app
from app.ratelimit import RateLimiter


class NoDocuments:
    async def list_documents(self, user: object) -> list:
        return []


def test_window_slides_per_key(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr("app.ratelimit.time.monotonic", lambda: now[0])
    limiter = RateLimiter(limit=2, window_seconds=60)

    assert limiter.retry_after("acme") is None
    assert limiter.retry_after("acme") is None
    assert limiter.retry_after("acme") == 61  # over the limit: told how long to wait
    assert limiter.retry_after("globex") is None  # other tenants have their own budget
    now[0] = 61.0
    assert limiter.retry_after("acme") is None  # window slid past the old hits


def test_api_limits_each_tenant_independently(token_for) -> None:
    limited = Settings(
        jwt_secret_key="test-secret-that-is-at-least-thirty-two-bytes",
        tenant_rate_limit_per_minute=2,
    )
    app.dependency_overrides[get_settings] = lambda: limited
    app.dependency_overrides[get_retrieval_service] = lambda: NoDocuments()
    client = TestClient(app)
    acme = {"Authorization": f"Bearer {token_for('bob', 'acme')}"}
    acme_other_user = {"Authorization": f"Bearer {token_for('carol', 'acme')}"}
    globex = {"Authorization": f"Bearer {token_for('dave', 'globex')}"}

    codes = [client.get("/documents", headers=acme).status_code for _ in range(2)]
    blocked = client.get("/documents", headers=acme_other_user)  # same tenant, different user
    other_tenant = client.get("/documents", headers=globex)

    app.dependency_overrides.clear()
    assert codes == [200, 200]
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert other_tenant.status_code == 200
