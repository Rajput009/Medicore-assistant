"""MediCore auth service: local login stub + OIDC SSO + internal token minting."""

import os
import secrets
from typing import Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from backend.common.config import settings
from backend.common.middleware import AuditLogMiddleware
from backend.common.security import create_access_token
from backend.common.telemetry import instrument_fastapi

app = instrument_fastapi(
    FastAPI(title="MediCore Auth", version="0.1.0"), service_name="auth"
)

# Session cookie is required for the OIDC state/nonce round-trip.
_session_secret = os.getenv("SESSION_SECRET", settings.session_secret)
if settings.env not in ("local", "test") and _session_secret in (
    "dev-change-me",
    "",
):
    raise RuntimeError(
        "SESSION_SECRET must be set to a strong random value outside local/test"
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=settings.env not in ("local", "test"),
)

# Credentialed CORS cannot use a "*" origin — browsers reject the combination,
# so fall back to an explicit allow-list.
_origins = settings.cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.add_middleware(AuditLogMiddleware)


class Health(BaseModel):
    status: str
    service: str
    env: str


@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", service="auth", env=settings.env)


# --------------------------------------------------------------------------
# Local (development) login
# --------------------------------------------------------------------------


class LoginReq(BaseModel):
    username: str
    password: str


class TokenResp(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 3600


def _demo_login_enabled() -> bool:
    """The username/password stub authenticates nobody — keep it out of prod."""
    return settings.env in ("local", "test") or os.getenv(
        "ENABLE_DEMO_LOGIN", ""
    ).lower() in ("1", "true", "yes")


@app.post("/login", response_model=TokenResp)
def login(req: LoginReq) -> TokenResp:
    if not _demo_login_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Local login is disabled; use /oidc/login",
        )
    if not req.username or not req.password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    demo_password = os.getenv("DEMO_PASSWORD", "medicore-dev")
    # Constant-time compare avoids leaking the password via timing.
    if not secrets.compare_digest(req.password, demo_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    token = create_access_token(sub=req.username, roles=["clinician"])
    return TokenResp(access_token=token)


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

    internal = create_access_token(sub=str(sub), roles=_map_roles(userinfo))
    return JSONResponse(
        {
            "access_token": internal,
            "token_type": "bearer",
            "user": {
                "sub": sub,
                "email": userinfo.get("email"),
                "name": userinfo.get("name"),
            },
        }
    )
