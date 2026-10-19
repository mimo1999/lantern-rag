from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class UserContext:
    """Identity claims verified by the API, never supplied by a request body."""

    user_id: str
    tenant_id: str
    roles: tuple[str, ...]
    # Token identity, used for revocation (sign-out) checks.
    token_id: str = field(default="", compare=False)
    issued_at: int = field(default=0, compare=False)
    expires_at: int = field(default=0, compare=False)
