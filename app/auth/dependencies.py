"""
FastAPI dependencies for authenticating requests and enforcing roles.

Supports two token sources so the same auth works for both the JSON API
and the server-rendered dashboard:
  - `Authorization: Bearer <token>` header (API clients)
  - `access_token` httponly cookie (the dashboard, set on login)
"""
from __future__ import annotations

from fastapi import Cookie, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.auth.security import TokenError, decode_access_token
from app.db.models import User, UserRole
from app.db.session import get_db


def _extract_token(authorization: str | None, access_token_cookie: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:]
    return access_token_cookie


async def get_current_user(
    authorization: str | None = Header(default=None),
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    token = _extract_token(authorization, access_token)
    if not token:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})

    try:
        payload = decode_access_token(token)
    except TokenError as e:
        raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"})

    user = db.get(User, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(401, "Account not found or disabled")
    return user


def _role_allowed(user_role: UserRole, allowed_roles: tuple[UserRole, ...]) -> bool:
    """Pure decision logic, factored out so it's unit-testable without
    needing fastapi installed (see tests/test_auth.py)."""
    return user_role in allowed_roles


def require_role(*allowed_roles: UserRole):
    """Dependency factory: `Depends(require_role(UserRole.ADMIN))`."""

    async def _check(user: User = Depends(get_current_user)) -> User:
        if not _role_allowed(user.role, allowed_roles):
            raise HTTPException(
                403,
                f"This action requires one of: {[r.value for r in allowed_roles]}. "
                f"Your role: {user.role.value}.",
            )
        return user

    return _check


# Convenience shorthand for "any authenticated internal user" -- covers
# the common case of "just needs to be logged in, any role."
require_authenticated = require_role(UserRole.ADMIN, UserRole.LOAN_OFFICER, UserRole.PROCESSOR)
