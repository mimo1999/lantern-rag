from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.documents import router as documents_router
from app.api.query import router as query_router
from app.config import get_settings
from app.db.pool import close_pool
from app.observability import metrics
from app.observability.logging import configure_logging
from app.observability.middleware import RequestContextMiddleware

WEB_DIR = Path(__file__).parent / "web"

_UI_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


class UIStaticFiles(StaticFiles):
    """Serves the chat UI with a strict CSP; API docs at /docs keep their own defaults."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(_UI_HEADERS)
        return response


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await close_pool()


_settings = get_settings()
configure_logging(_settings.log_level)

app = FastAPI(title="Permission-Aware Multi-Tenant RAG", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)
app.include_router(metrics.router)
app.include_router(auth_router)
app.include_router(audit_router)
app.include_router(query_router)
app.include_router(documents_router)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Mounted last so every API route above takes precedence over the static catch-all.
if WEB_DIR.is_dir():
    app.mount("/", UIStaticFiles(directory=WEB_DIR, html=True), name="ui")
