from contextvars import ContextVar
from uuid import uuid4

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str:
    """FastAPI dependency: the id assigned by the request middleware (or a fresh one)."""
    return request_id_var.get() or str(uuid4())
