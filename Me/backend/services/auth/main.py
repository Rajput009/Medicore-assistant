"""MediCore auth service: local login stub + OIDC SSO + internal token minting.

Issues short-lived access tokens and, for browser clients, an httpOnly Secure
session cookie so the SPA never has to hold the JWT in JavaScript-accessible
storage (XSS cannot exfiltrate it). Logout revokes the token's ``jti`` so the
credential dies before natural expiry.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from backend.common.app import create_service_app
from backend.common.config import settings
from backend.common.csrf import clear_csrf_cookie, issue_csrf_cookie
from backend.common.revocation import revoke_payload
from backend.common.security import create_access_token, verify_access_token

# Session cookie is required for the OIDC state/nonce round-trip.
_session_secret = os.getenv("SESSION_SECRET", settings.session_secret)
if settings.is_production and (
    not _session_secret or _session_secret in ("dev-change-me", "")
):
    raise RuntimeError(
        "SESSION_SECRET must be set to a strong random value in production"
    )

# Session middleware must sit inside CORS/TrustedHost so the cookie is set on
# responses that already passed host/origin checks. Registered as innermost
# extra middleware via the shared factory.
app = create_service_app(
    title="MediCore Auth",
    service_name="auth",
    version="1.0.0",
    rate_limit=settings.login_rate_limit_per_minute,
    enable_cors=True,
    extra_middleware=(
        (
            SessionMiddleware,
            {
                "secret_key": _session_secret,
                "same_site": "lax",
                "https_only": settings.is_production,
                "max_age": 600,  # OIDC round-trip only; short-lived.
            },
        ),
    ),
)


class Health(BaseModel):
    status: str
    service: str
    env: str


@app.get("/health", response_model=Health, tags=["ops"])
def health() -> Health:
    return Health(status="ok", service="auth", env=settings.env)


@app.get("/ready", tags=["ops"])
def ready() -> dict[str, Any]:
    """Readiness reports whether SSO is wired up; the service still serves
    token verification for existing sessions either way."""
    return {"status": "ok", "oidc": "configured" if _OIDC_CONFIGURED else "disabled"}


# --------------------------------------------------------------------------
# Cookie helpers
# --------------------------------------------------------------------------


def _cookie_secure() -> bool:
    # Secure cookies on anything that is not a plain local/test HTTP setup.
    return settings.is_production or settings.env.lower() not in ("local", "test")


def _set_session_cookie(response: Response, token: str, max_age: int) -> None:
    if not settings.auth_set_cookie:
        return
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
    )


def _extract_bearer_or_cookie(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    cookie = request.cookies.get(settings.auth_cookie_name)
    if cookie and cookie.strip():
        return cookie.strip()
    return None


# --------------------------------------------------------------------------
# Local (development) login
# --------------------------------------------------------------------------


class LoginReq(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=256)


class TokenResp(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def _demo_login_enabled() -> bool:
    """The username/password stub authenticates nobody real — keep it out of prod.

    Production is always False (enforced in Settings too). Outside production
    the shared ``settings.demo_login_allowed`` gate still requires ENV=local/test
    or an explicit ENABLE_DEMO_LOGIN opt-in.
    """
    return settings.demo_login_allowed


@app.post("/login", response_model=TokenResp)
def login(req: LoginReq, response: Response) -> TokenResp:
    if not _demo_login_enabled():
        # 404 rather than 403: do not advertise that a password endpoint exists.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Local login is disabled; use /oidc/login",
        )
    if not req.username.strip() or not req.password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    demo_password = os.getenv("DEMO_PASSWORD", "medicore-dev")
    # Constant-time compare avoids leaking the password via timing.
    if not secrets.compare_digest(req.password, demo_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    ttl = settings.access_token_ttl_minutes
    token = create_access_token(sub=req.username.strip(), roles=["clinician"])
    body = TokenResp(access_token=token, expires_in=ttl * 60)
    _set_session_cookie(response, token, max_age=ttl * 60)
    issue_csrf_cookie(response, secure=_cookie_secure())
    return body


@app.post("/logout", tags=["auth"])
def logout(request: Request, response: Response) -> dict[str, str]:
    """Revoke the current access token and clear the session cookie.

    Safe to call anonymously: a missing/invalid token still clears the cookie
    so a half-broken browser session can always recover.
    """
    raw = _extract_bearer_or_cookie(request)
    if raw:
        try:
            payload = verify_access_token(raw)
            revoke_payload(payload)
        except Exception:
            # Token already invalid/expired — still clear the cookie.
            pass
    _clear_session_cookie(response)
    clear_csrf_cookie(response, secure=_cookie_secure())
    return {"status": "ok"}


@app.get("/session", tags=["auth"])
def session(request: Request) -> dict[str, Any]:
    """Return the caller's claims without exposing the raw token.

    The SPA uses this after a cookie-based login so it never has to read the
    JWT out of storage. Returns 401 when no valid session is present.
    """
    raw = _extract_bearer_or_cookie(request)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = verify_access_token(raw)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    return {
        "sub": payload.get("sub"),
        "roles": payload.get("roles") or [],
        "exp": payload.get("exp"),
        "jti": payload.get("jti"),
    }


class EstablishSessionReq(BaseModel):
    """One-shot handoff from an OIDC fragment / external token mint."""

    access_token: str = Field(..., min_length=20, max_length=8192)


@app.post("/session/establish", tags=["auth"])
def establish_session(
    req: EstablishSessionReq, response: Response
) -> dict[str, Any]:
    """Verify ``access_token`` and set the httpOnly session cookie.

    Used by the SPA after an OIDC redirect that delivered the token in the
    URL fragment. The browser then discards the fragment; only the cookie
    remains. Returns claims (never echoes the raw token back for storage).
    """
    try:
        payload = verify_access_token(req.access_token.strip())
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    exp = int(payload.get("exp") or 0)
    now = int(__import__("time").time())
    max_age = max(1, exp - now) if exp else settings.access_token_ttl_minutes * 60
    _set_session_cookie(response, req.access_token.strip(), max_age=max_age)
    issue_csrf_cookie(response, secure=_cookie_secure())
    return {
        "sub": payload.get("sub"),
        "roles": payload.get("roles") or [],
        "exp": payload.get("exp"),
        "jti": payload.get("jti"),
    }


# --------------------------------------------------------------------------
# OIDC SSO
# --------------------------------------------------------------------------

oauth = OAuth()

_issuer_meta = os.getenv("OIDC_ISSUER", settings.oidc_issuer)
_client_id = os.getenv("OIDC_CLIENT_ID", settings.oidc_client_id)
_client_secret = os.getenv("OIDC_CLIENT_SECRET", settings.oidc_client_secret)
_redirect_uri = os.getenv("OIDC_REDIRECT_URI", settings.oidc_redirect_uri)

_OIDC_CONFIGURED = bool(_issuer_meta and _client_id and _client_secret)

if _OIDC_CONFIGURED:
    metadata_url = _issuer_meta
    # Accept either a bare issuer or a full discovery URL.
    if not metadata_url.endswith("/.well-known/openid-configuration"):
        metadata_url = (
            metadata_url.rstrip("/") + "/.well-known/openid-configuration"
        )
    oauth.register(
        name="idp",
        server_metadata_url=metadata_url,
        client_id=_client_id,
        client_secret=_client_secret,
        client_kwargs={"scope": "openid email profile"},
    )


def _not_configured() -> JSONResponse:
    return JSONResponse({"error": "OIDC not configured"}, status_code=501)


# Map IdP groups/roles onto internal roles.
_ROLE_MAP = {
    "medicore-admin": "admin",
    "medicore-clinician": "clinician",
}


def _map_roles(userinfo: dict[str, Any]) -> list[str]:
    raw: list[str] = []
    for claim in ("roles", "groups"):
        value = userinfo.get(claim)
        if isinstance(value, str):
            raw.extend(value.replace(",", " ").split())
        elif isinstance(value, (list, tuple)):
            raw.extend(str(v) for v in value)
    roles = {_ROLE_MAP[r] for r in raw if r in _ROLE_MAP}
    # Default to the least-privileged role rather than granting clinician.
    return sorted(roles) or ["viewer"]


@app.get("/oidc/login")
async def oidc_login(request: Request):
    if not _OIDC_CONFIGURED:
        return _not_configured()
    return await oauth.idp.authorize_redirect(request, _redirect_uri)


@app.get("/oidc/callback")
async def oidc_callback(request: Request):
    if not _OIDC_CONFIGURED:
        return _not_configured()
    try:
        token = await oauth.idp.authorize_access_token(request)
    except OAuthError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # authorize_access_token already parses and validates the id_token when the
    # provider returns one; calling parse_id_token(request, token) again fails
    # (it requires a nonce and has a different signature across Authlib
    # versions). Prefer the parsed claims, then fall back to /userinfo.
    userinfo: dict[str, Any] | None = token.get("userinfo")
    if not userinfo:
        try:
            userinfo = dict(await oauth.idp.userinfo(token=token))
        except Exception as exc:
            return JSONResponse(
                {"error": f"unable to fetch userinfo: {exc}"}, status_code=400
            )

    sub = userinfo.get("sub") or userinfo.get("email")
    if not sub:
        return JSONResponse(
            {"error": "IdP response contained no subject"}, status_code=400
        )

    ttl = settings.access_token_ttl_minutes
    internal = create_access_token(sub=str(sub), roles=_map_roles(userinfo))
    body = {
        "access_token": internal,
        "token_type": "bearer",
        "expires_in": ttl * 60,
        "user": {
            "sub": sub,
            "email": userinfo.get("email"),
            "name": userinfo.get("name"),
        },
    }
    response = JSONResponse(body)
    _set_session_cookie(response, internal, max_age=ttl * 60)
    issue_csrf_cookie(response, secure=_cookie_secure())
    return response
