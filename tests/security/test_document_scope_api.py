from datetime import UTC, datetime, timedelta

import jwt
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.dependencies import get_retrieval_service
from app.ingestion.dependencies import get_ingestion_service
from app.ingestion.service import IngestionResult, NewDocument
from app.main import app
from app.retrieval.service import DocumentSummary

settings = Settings(jwt_secret_key="test-secret-that-is-at-least-thirty-two-bytes")


class RecordingIngestion:
    def __init__(self) -> None:
        self.indexed: list[NewDocument] = []

    async def ingest(self, document: NewDocument) -> IngestionResult:
        self.indexed.append(document)
        return IngestionResult("doc-id", 1, duplicate=False)


class FakeRetrieval:
    def __init__(self, documents: list[DocumentSummary]) -> None:
        self._documents = documents

    async def list_documents(self, user: object) -> list[DocumentSummary]:
        return self._documents


def make_token(**claims: object) -> str:
    payload = {
        "sub": "bob",
        "tenant_id": "acme",
        "roles": ["admin"],
        "iss": settings.jwt_issuer,
        "jti": "test-jti",
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        **claims,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")


def test_tenant_document_upload_is_scoped_to_caller_tenant() -> None:
    indexer = RecordingIngestion()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_ingestion_service] = lambda: indexer
    client = TestClient(app)

    response = client.post(
        "/documents",
        json={"title": "Finance Report", "content": "text", "allowed_roles": ["admin"]},
        headers={"Authorization": f"Bearer {make_token()}"},
    )

    app.dependency_overrides.clear()
    assert response.status_code == 201
    assert indexer.indexed[0].scope == "tenant"
    assert indexer.indexed[0].tenant_id == "acme"


def test_tenant_admin_cannot_upload_default_scope_documents() -> None:
    indexer = RecordingIngestion()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_ingestion_service] = lambda: indexer
    client = TestClient(app)

    response = client.post(
        "/documents/default",
        json={"title": "Handbook", "content": "text", "allowed_roles": ["admin"]},
        headers={"Authorization": f"Bearer {make_token(roles=['admin'])}"},
    )

    app.dependency_overrides.clear()
    assert response.status_code == 403
    assert indexer.indexed == []


def test_platform_admin_can_upload_default_scope_documents() -> None:
    indexer = RecordingIngestion()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_ingestion_service] = lambda: indexer
    client = TestClient(app)

    response = client.post(
        "/documents/default",
        json={"title": "Handbook", "content": "text", "allowed_roles": ["admin"]},
        headers={"Authorization": f"Bearer {make_token(roles=['platform-admin'])}"},
    )

    app.dependency_overrides.clear()
    assert response.status_code == 201
    assert indexer.indexed[0].scope == "default"
    assert indexer.indexed[0].tenant_id is None


def test_list_documents_requires_authentication() -> None:
    client = TestClient(app)

    response = client.get("/documents")

    assert response.status_code == 401


def test_list_documents_returns_visible_documents_for_authenticated_user() -> None:
    retrieval = FakeRetrieval(
        [
            DocumentSummary(
                document_id="handbook",
                title="Employee Handbook",
                scope="default",
                tenant_id=None,
                source_uri=None,
                allowed_roles=("admin", "employee"),
            ),
            DocumentSummary(
                document_id="acme-finance",
                title="Acme Finance",
                scope="tenant",
                tenant_id="acme",
                source_uri=None,
                allowed_roles=("admin", "finance"),
            ),
        ]
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval
    client = TestClient(app)

    response = client.get(
        "/documents",
        headers={"Authorization": f"Bearer {make_token()}"},
    )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    body = response.json()
    assert {document["document_id"] for document in body} == {"handbook", "acme-finance"}
    assert {document["scope"] for document in body} == {"default", "tenant"}
