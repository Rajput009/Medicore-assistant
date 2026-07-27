"""Dependency-injection helpers for authentication and RBAC."""

from collections.abc import Sequence
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from backend.common.security import verify_access_token

bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED = {"WWW-Authenticate": "Bearer"}


class User(BaseModel):
    sub: str
    roles: list[str] = []
    claims: dict[str, Any] = {}

    def has_any_role(self, required: Sequence[str]) -> bool:
        return bool(set(required) & set(self.roles))


def _coerce_roles(raw: Any) -> list[str]:
    """Normalise the roles claim, which IdPs emit in several shapes."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [r for r in raw.replace(",", " ").split() if r]
    if isinstance(raw, (list, tuple, set)):
        return [str(r) for r in raw]
    return []


def _user_from_claims(payload: dict[str, Any]) -> User:
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers=_UNAUTHORIZED,
        )
    return User(sub=str(sub), roles=_coerce_roles(payload.get("roles")), claims=payload)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> User:
    """Resolve the caller.

    Reuses the claims already verified by ``JWTAuthMiddleware`` when present so
    the token is not parsed and verified twice per request.
    """
    state_user = getattr(request.state, "user", None)
    if state_user:
        return _user_from_claims(state_user.get("claims") or state_user)

    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid credentials",
            headers=_UNAUTHORIZED,
        )
    try:
        payload = verify_access_token(credentials.credentials)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers=_UNAUTHORIZED,
        ) from None
    return _user_from_claims(payload)


def requires_roles(*required: str):
    """FastAPI dependency factory enforcing that the caller has one of ``required``.

    Usage::

        @app.get("/x", dependencies=[Depends(requires_roles("admin"))])

    or to also receive the user::

        def handler(user: User = Depends(requires_roles("admin"))): ...
    """
    allowed = [r for r in required]

    def dependency(user: User = Depends(get_current_user)) -> User:
        if allowed and not user.has_any_role(allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient role",
            )
        return user

    return dependency


# Common role bundles used across the gateway's FHIR routes.
def clinician_or_admin(
    user: User = Depends(requires_roles("clinician", "admin")),
) -> User:
    return user


def admin_only(user: User = Depends(requires_roles("admin"))) -> User:
    return user
