import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.auth.authentication import get_current_user
from app.auth.dependencies import get_user_repository
from app.auth.mail import Mailer, get_mailer
from app.auth.models import UserContext
from app.auth.passwords import hash_password, verify_password
from app.auth.revocation import TokenRevocations, get_token_revocations
from app.auth.tokens import issue_access_token
from app.auth.users import UserRepository
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

LOCKED_DETAIL = (
    "This account is locked after too many failed sign-ins. "
    "Use 'Forgot password' to reset it by email."
)


class LoginRequest(BaseModel):
    # tenant_id only selects whose username namespace to check; authorization still comes
    # from the stored user record, never from this field.
    tenant_id: str = Field(min_length=1, max_length=64)
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1_000)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def _locked() -> HTTPException:
    return HTTPException(status_code=status.HTTP_423_LOCKED, detail=LOCKED_DETAIL)


@router.post("/login", response_model=TokenResponse)
async def login(
    request: LoginRequest,
    users: UserRepository = Depends(get_user_repository),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    user = await users.find(request.tenant_id, request.username)
    if user is not None and user.is_locked:
        raise _locked()
    password_ok = verify_password(request.password, user.password_hash if user else None)
    if user is None or not user.is_active or not password_ok:
        # Unknown and inactive users are not counted, so the response never reveals them.
        if user is not None and user.is_active:
            if await users.record_failed_login(user.id, settings.login_max_attempts):
                logger.warning("account_locked", extra={"user_id": user.id})
                raise _locked()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not await users.record_successful_login(user.id):  # locked by a racing attempt
        raise _locked()
    token, expires_in = issue_access_token(user, settings)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    user: UserContext = Depends(get_current_user),
    revocations: TokenRevocations = Depends(get_token_revocations),
) -> Response:
    """Revoke the presented token so it stops working before it expires."""
    await revocations.revoke(user.token_id, user.expires_at)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class ResetRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    # Matched exactly against the stored address, so no format validation is needed.
    email: str = Field(min_length=3, max_length=320)


class ResetConfirm(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=12, max_length=1_000)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    request: ResetRequest,
    users: UserRepository = Depends(get_user_repository),
    mailer: Mailer = Depends(get_mailer),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Always answers the same way so it cannot be used to discover which emails exist."""
    user_id = await users.find_active_id_by_email(request.tenant_id, request.email)
    if user_id is not None:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(minutes=settings.password_reset_ttl_minutes)
        if await users.create_password_reset(user_id, _hash_token(token), expires):
            link = f"{settings.public_base_url.rstrip('/')}/app/#reset={token}"
            await mailer.send_password_reset(request.email, link)
    return {"detail": "If that address is registered, a reset link has been sent."}


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(
    request: ResetConfirm, users: UserRepository = Depends(get_user_repository)
) -> Response:
    if not await users.complete_password_reset(
        _hash_token(request.token), hash_password(request.new_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This reset link is invalid or expired."
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
