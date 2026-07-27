"""Global JWT enforcement for the gateway.

Note: raising ``HTTPException`` inside ``BaseHTTPMiddleware.dispatch`` does not
produce a 401 — it escapes FastAPI's exception handlers and surfaces as a 500.
The middleware therefore returns ``JSONResponse`` directly.
"""

from collections.abc import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_401_UNAUTHORIZED

from backend.common.config import settings
from backend.common.security import verify_access_token

# Exact-match public paths (prefix matching would let "/healthz-admin" or
# "/docsecret" through).
#
# /ready MUST stay public: Kubernetes readiness probes and Docker HEALTHCHECK
# do not send Authorization headers. It only reports dependency status and
# never returns PHI.
#
# /docs, /redoc and /openapi.json are only exempt when the deployment has
# explicitly enabled API docs (local/test). In production they are disabled at
# the FastAPI layer AND require auth here as defence in depth.
_ALWAYS_PUBLIC = frozenset(
    {
        "/health",
        "/healthz",
        "/ready",
        "/favicon.ico",
        "/metrics",
    }
)

_DOCS_PATHS = frozenset(
    {
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)


def public_paths() -> frozenset[str]:
    """Paths that skip JWT verification for the current configuration."""
    paths = set(_ALWAYS_PUBLIC)
    if settings.expose_api_docs and not settings.is_production:
        paths.update(_DOCS_PATHS)
    return frozenset(paths)


# Backwards-compatible name used by tests and older imports.
EXEMPT_PATHS = public_paths()


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"detail": detail},
        status_code=HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )


class JWTAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, exempt_paths: Iterable[str] | None = None):
        super().__init__(app)
        # Resolve at construction time so tests that flip settings before
        # building the app get the right set; production builds once at import.
        self.exempt_paths = frozenset(
            exempt_paths if exempt_paths is not None else public_paths()
        )

    async def dispatch(self, request: Request, call_next) -> Response:
        # CORS preflight carries no Authorization header by design.
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path.rstrip("/") or "/"
        if path in {p.rstrip("/") or "/" for p in self.exempt_paths}:
            return await call_next(request)

        auth = request.headers.get("authorization")
        if not auth:
            return _unauthorized("missing auth header")

        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return _unauthorized("invalid authorization scheme")

        try:
            payload = verify_access_token(token.strip())
        except Exception:
            # Never echo the exception: it can leak token/key details.
            return _unauthorized("token invalid or expired")

        request.state.user = {
            "sub": payload.get("sub"),
            "roles": payload.get("roles", []),
            "claims": payload,
        }
        return await call_next(request)
