"""Shared authentication / RBAC dependencies for the internal services.

The gateway has its own middleware, but services that can be reached directly
(patient_flow, cds) need their own enforcement: in a Kubernetes cluster any pod
can talk to any Service, so "it sits behind the gateway" is not a control.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .security import verify_access_token

bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}

VALID_ROLES = ("admin", "clinician", "viewer")


class Principal(BaseModel):
    """The authenticated caller."""

    sub: str
    roles: list[str] = []
    claims: dict[str, Any] = {}
    # Optional ward / department scope from IdP claims (e.g. wards=["A","ICU"]).
    wards: list[str] = []
    departments: list[str] = []

    def has_any_role(self, required: tuple[str, ...] | list[str]) -> bool:
        return bool(set(required) & set(self.roles))

    def can_access_ward(self, ward: str | None) -> bool:
        """True when the principal may touch data for ``ward``.

        Empty scope = unrestricted (legacy tokens / admins without claims).
        Admins always pass. Otherwise the ward must appear in ``wards``.
        """
        if not ward:
            return True
        if "admin" in self.roles:
            return True
        if not self.wards:
            return True
        return ward in self.wards

    def can_access_department(self, dept: str | None) -> bool:
        if not dept:
            return True
        if "admin" in self.roles:
            return True
        if not self.departments:
            return True
        return dept in self.departments

    def can_access_patient(self, patient_id: str | None, *, assigned: set[str] | None = None) -> bool:
        """Patient-level gate.

        - Admins: always.
        - If ``assigned`` is provided (e.g. from an encounter service), the
          patient must be in that set unless the caller is admin.
        - Otherwise fall through (role-only) — full chart ACL needs an
          encounter index; this hook is the extension point.
        """
        if not patient_id:
            return True
        if "admin" in self.roles:
            return True
        if assigned is not None:
            return patient_id in assigned
        return True


def normalise_roles(raw: Any) -> list[str]:
    """IdPs emit roles as a list or as a delimited string; accept both."""
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates: list[str] = [r for r in raw.replace(",", " ").split() if r]
    elif isinstance(raw, (list, tuple, set)):
        candidates = [str(r) for r in raw]
    else:
        return []

    seen: list[str] = []
    for value in candidates:
        lowered = value.strip().lower()
        if lowered in VALID_ROLES and lowered not in seen:
            seen.append(lowered)
    return seen


def _string_list_claim(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(v).strip() for v in raw if str(v).strip()]
    return []


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers=_UNAUTHORIZED_HEADERS,
    )


def _token_from_request(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    if credentials and credentials.credentials:
        return credentials.credentials
    # httpOnly session cookie set by the auth service on login/OIDC callback.
    try:
        from .config import settings

        cookie = request.cookies.get(settings.auth_cookie_name)
        if cookie and cookie.strip():
            return cookie.strip()
    except Exception:
        pass
    return None


def get_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    """Verify the bearer token (or session cookie) and return the caller.

    Reuses claims already validated by upstream middleware (the gateway) when
    present, so a token is not verified twice on the same request.
    """
    state_user = getattr(request.state, "user", None)
    if state_user:
        claims = state_user.get("claims") or state_user
    else:
        raw = _token_from_request(request, credentials)
        if not raw:
            raise _unauthorized("Missing bearer token")
        try:
            claims = verify_access_token(raw)
        except Exception:
            # Never echo the underlying error: it can leak key/token details.
            raise _unauthorized("Invalid or expired token") from None

    sub = claims.get("sub")
    if not sub:
        raise _unauthorized("Invalid token")

    return Principal(
        sub=str(sub),
        roles=normalise_roles(claims.get("roles")),
        claims=claims,
        wards=_string_list_claim(claims.get("wards") or claims.get("ward")),
        departments=_string_list_claim(
            claims.get("departments") or claims.get("depts") or claims.get("dept")
        ),
    )


def require_ward_access(ward: str | None, principal: Principal) -> None:
    if not principal.can_access_ward(ward):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorised for this ward",
        )


def require_department_access(dept: str | None, principal: Principal) -> None:
    if not principal.can_access_department(dept):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorised for this department",
        )


def requires_roles(*required: str):
    """Dependency factory enforcing that the caller holds one of ``required``."""
    allowed = tuple(required)

    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if allowed and not principal.has_any_role(allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient role",
            )
        return principal

    return dependency


def clinical_staff(
    principal: Principal = Depends(requires_roles("clinician", "admin")),
) -> Principal:
    """Caller may read/write patient data."""
    return principal


def any_authenticated(principal: Principal = Depends(get_principal)) -> Principal:
    """Caller only needs a valid token (no specific role)."""
    return principal
