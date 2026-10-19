import json
import logging
from uuid import UUID

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.audit.dependencies import get_audit_service
from app.auth.models import UserContext
from app.config import Settings, get_settings
from app.generation.dependencies import get_answer_service
from app.generation.service import AnswerResult
from app.main import app
from app.observability.logging import JsonFormatter

SETTINGS = Settings(jwt_secret_key="test-secret-that-is-at-least-thirty-two-bytes")


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_json_formatter_emits_extras_and_request_id() -> None:
    from app.observability.context import request_id_var

    token = request_id_var.set("req-1")
    try:
        record = logging.LogRecord("app.x", logging.INFO, __file__, 1, "did_thing", None, None)
        record.tenant_id = "acme"
        line = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)

    assert line["event"] == "did_thing" and line["level"] == "INFO"
    assert line["tenant_id"] == "acme" and line["request_id"] == "req-1"


def test_middleware_assigns_and_honors_request_ids() -> None:
    client = TestClient(app)
    supplied = "5f0f2f0e-6c1f-4f0e-9d3e-1a2b3c4d5e6f"

    honored = client.get("/health", headers={"X-Request-ID": supplied})
    replaced = client.get("/health", headers={"X-Request-ID": "not-a-uuid\r\nInjected: 1"})
    generated = client.get("/health")

    assert honored.headers["x-request-id"] == supplied
    replaced_id = replaced.headers["x-request-id"]
    assert UUID(replaced_id) and replaced_id != "not-a-uuid"
    assert UUID(generated.headers["x-request-id"])


def test_http_metrics_use_route_templates_and_count_denials() -> None:
    client = TestClient(app)
    before_denied = sample("authorization_denied_total", status="401")
    before_docs = sample(
        "http_requests_total", method="GET", route="/documents", status="401"
    )

    client.get("/documents")
    client.get("/definitely/not/a/route/123")

    assert sample("authorization_denied_total", status="401") == before_denied + 1
    assert sample(
        "http_requests_total", method="GET", route="/documents", status="401"
    ) == before_docs + 1
    assert sample("http_requests_total", method="GET", route="other", status="404") >= 1
    assert "/definitely/not/a/route/123" not in TestClient(app).get("/metrics").text


def test_metrics_endpoint_requires_token_when_configured() -> None:
    secured = Settings(
        jwt_secret_key="test-secret-that-is-at-least-thirty-two-bytes", metrics_token="s3cr3t"
    )
    app.dependency_overrides[get_settings] = lambda: secured
    try:
        client = TestClient(app)
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = client.get("/metrics", headers={"Authorization": "Bearer s3cr3t"})
    finally:
        app.dependency_overrides.clear()

    assert ok.status_code == 200 and "http_requests_total" in ok.text


def test_query_response_id_matches_request_id_header(token_for) -> None:
    class Answers:
        async def answer(self, question: str, user: UserContext) -> AnswerResult:
            return AnswerResult("no", [], "no_results", None, None, None, 1.0, None)

    class Audit:
        async def record_answer(self, *args: object) -> None: ...

    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_answer_service] = lambda: Answers()
    app.dependency_overrides[get_audit_service] = lambda: Audit()
    before = sample("rag_queries_total", outcome="no_results")
    try:
        response = TestClient(app).post(
            "/query",
            json={"query": "q"},
            headers={"Authorization": f"Bearer {token_for()}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert sample("rag_queries_total", outcome="no_results") == before + 1
