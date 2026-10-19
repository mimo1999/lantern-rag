from datetime import UTC, datetime, timedelta

import jwt
from fastapi.testclient import TestClient

from app.audit.dependencies import get_audit_service
from app.config import Settings, get_settings
from app.dependencies import get_retrieval_service
from app.generation.dependencies import get_llm_provider
from app.generation.provider import ChatMessage, Generation
from app.main import app
from app.retrieval.service import RetrievalService

settings = Settings(jwt_secret_key="test-secret-that-is-at-least-thirty-two-bytes")


class NothingRetriever:
    async def search(self, query, authorization_filter, limit):
        return []


class NullAudit:
    async def record_answer(self, *args: object) -> None: ...

    async def record_error(self, *args: object) -> None: ...


class UnusedProvider:
    async def generate(self, messages: list[ChatMessage]) -> Generation:
        raise AssertionError("the LLM must not be called when nothing was retrieved")


def make_token(**claims: object) -> str:
    payload = {
        "sub": "bob",
        "tenant_id": "acme",
        "roles": ["employee"],
        "iss": settings.jwt_issuer,
        "jti": "test-jti",
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        **claims,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")


def test_query_rejects_missing_token() -> None:
    client = TestClient(app)

    response = client.post("/query", json={"query": "confidential information"})

    assert response.status_code == 401


def test_query_uses_verified_identity_not_request_body() -> None:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_retrieval_service] = lambda: RetrievalService(
        NothingRetriever(), 20
    )
    app.dependency_overrides[get_llm_provider] = lambda: UnusedProvider()
    app.dependency_overrides[get_audit_service] = lambda: NullAudit()
    client = TestClient(app)

    response = client.post(
        "/query",
        json={"query": "engineering overview", "tenant_id": "globex", "roles": ["admin"]},
        headers={"Authorization": f"Bearer {make_token()}"},
    )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["sources"] == []
    assert response.json()["answer"] == "No accessible information was found."
