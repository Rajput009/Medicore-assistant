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

from backend.common.security import verify_access_token

# Exact-match public paths (prefix matching would let "/healthz-admin" or
# "/docsecret" through).
EXEMPT_PATHS = frozenset(
    {
        "/health",
        "/healthz",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
        "/metrics",
    }
)


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"detail": detail},
        status_code=HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )


class JWTAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, exempt_paths: Iterable[str] = EXEMPT_PATHS):
        super().__init__(app)
        self.exempt_paths = frozenset(exempt_paths)

    async def dispatch(self, request: Request, call_next) -> Response:
        # CORS preflight carries no Authorization header by design.
        if request.method == "OPTIONS":
            return await call_next(request)

        if request.url.path.rstrip("/") in {p.rstrip("/") for p in self.exempt_paths}:
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
