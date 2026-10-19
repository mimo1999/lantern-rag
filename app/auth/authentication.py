import logging
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

from app.auth.models import UserContext
from app.auth.revocation import TokenRevocations, get_token_revocations
from app.config import Settings, get_settings
from app.ratelimit import get_rate_limiter

logger = logging.getLogger(__name__)
JWT_ALGORITHM = "HS256"
bearer_scheme = HTTPBearer(auto_error=False)


def decode_user_context(token: str, settings: Settings) -> UserContext:
    """Validate a JWT and extract the minimum authorization context."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[JWT_ALGORITHM],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "tenant_id", "roles", "jti", "iat"]},
        )
        roles = claims["roles"]
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            raise ValueError("roles must be a list of strings")
        return UserContext(
            user_id=str(claims["sub"]),
            tenant_id=str(claims["tenant_id"]),
            roles=tuple(roles),
            token_id=str(claims["jti"]),
            issued_at=int(claims["iat"]),
            expires_at=int(claims["exp"]),
        )
    except (InvalidTokenError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
    revocations: TokenRevocations = Depends(get_token_revocations),
) -> UserContext:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = decode_user_context(credentials.credentials, settings)
    # Rate limit by the verified tenant (unauthenticated callers cannot create limiter keys).
    if settings.tenant_rate_limit_per_minute > 0:
        wait = get_rate_limiter(settings.tenant_rate_limit_per_minute).retry_after(user.tenant_id)
        if wait is not None:
            logger.warning("tenant_rate_limited", extra={"tenant_id": user.tenant_id})
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests for this organization. Try again shortly.",
                headers={"Retry-After": str(wait)},
            )
    if await revocations.is_revoked(user.token_id, user.user_id, user.issued_at):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


PLATFORM_ADMIN_ROLE = "platform-admin"
"""Distinct from a tenant's own `admin` role so a tenant admin can never ingest
documents visible to other tenants (default-scope ingestion)."""


def require_role(required_role: str) -> Callable[..., UserContext]:
    """Require a server-verified role for endpoints such as document ingestion."""

    def check_role(user: UserContext = Depends(get_current_user)) -> UserContext:
        if required_role not in user.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return check_role
