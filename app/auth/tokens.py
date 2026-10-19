from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt

from app.auth.authentication import JWT_ALGORITHM
from app.auth.users import StoredUser
from app.config import Settings


def issue_access_token(user: StoredUser, settings: Settings) -> tuple[str, int]:
    """Sign a JWT carrying only the claims the API needs to authorize a request."""
    now = datetime.now(UTC)
    expires_in = settings.access_token_ttl_minutes * 60
    token = jwt.encode(
        {
            "sub": user.id,
            "tenant_id": user.tenant_id,
            "roles": list(user.roles),
            "iss": settings.jwt_issuer,
            "jti": str(uuid4()),
            "iat": now,
            "exp": now + timedelta(seconds=expires_in),
        },
        settings.jwt_secret_key,
        algorithm=JWT_ALGORITHM,
    )
    return token, expires_in
