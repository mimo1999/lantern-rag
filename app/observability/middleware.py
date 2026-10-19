import logging
import time
from uuid import UUID, uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import metrics
from app.observability.context import request_id_var

logger = logging.getLogger("app.request")
_REQUEST_ID_HEADER = "x-request-id"


def _accept_request_id(candidate: str | None) -> str:
    """Honor a caller-supplied id only if it is a UUID (it is stored in a UUID column)."""
    try:
        return str(UUID(candidate)) if candidate else str(uuid4())
    except ValueError:
        return str(uuid4())


def _route_label(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if path and route.__class__.__name__ == "APIRoute" else "other"


class RequestContextMiddleware:
    """Assigns a request id, emits one structured log line, and records HTTP metrics."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        request_id = _accept_request_id(headers.get(_REQUEST_ID_HEADER.encode(), b"").decode())
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[_REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - started
            route = _route_label(scope)
            method = scope["method"]
            metrics.HTTP_REQUESTS.labels(method, route, str(status_code)).inc()
            metrics.HTTP_LATENCY.labels(method, route).observe(duration)
            if status_code in (401, 403):
                metrics.AUTH_DENIED.labels(str(status_code)).inc()
            if route != "other" or status_code >= 400:
                logger.info(
                    "request_completed",
                    extra={
                        "method": method,
                        "route": route,
                        "status": status_code,
                        "duration_ms": round(duration * 1000, 1),
                    },
                )
            request_id_var.reset(token)
