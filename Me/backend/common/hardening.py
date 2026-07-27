"""Transport and abuse-protection middleware.

These are edge concerns that a reverse proxy may also handle, but relying on
that is a single point of failure: if the proxy is bypassed (port-forward, a
misrouted internal call, a mesh sidecar in permissive mode) the application
must still defend itself.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Static headers applied to every response.
SECURITY_HEADERS: dict[str, str] = {
    # Stop browsers guessing content types (XSS vector for JSON endpoints).
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # PHI must never be written to a shared or disk cache.
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
    # API responses never need to execute anything.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds hardening headers, including HSTS when served over TLS."""

    def __init__(self, app, hsts: bool = True, hsts_max_age: int = 31_536_000):
        super().__init__(app)
        self.hsts = hsts
        self.hsts_max_age = hsts_max_age

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        # Only advertise HSTS on requests that arrived over TLS, otherwise a
        # local HTTP dev setup becomes unreachable after one visit.
        forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        if self.hsts and forwarded_proto == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={self.hsts_max_age}; includeSubDomains",
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies before they are buffered."""

    def __init__(self, app, max_bytes: int = 1_048_576):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_bytes:
                    return JSONResponse(
                        {"detail": "Request body too large"}, status_code=413
                    )
            except ValueError:
                return JSONResponse(
                    {"detail": "Invalid Content-Length header"}, status_code=400
                )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window-per-caller rate limiting.

    In-process, so with N replicas the effective ceiling is N x limit. That is
    acceptable for its purpose here - blunting credential stuffing and runaway
    clients - but a shared Redis limiter is required if you need an exact
    global budget. Documented rather than silently assumed.
    """

    def __init__(
        self,
        app,
        limit: int = 120,
        window_seconds: int = 60,
        exempt_paths: tuple[str, ...] = ("/health", "/ready", "/metrics"),
        key_func: Callable[[Request], str] | None = None,
    ):
        super().__init__(app)
        self.limit = limit
        self.window = window_seconds
        self.exempt_paths = exempt_paths
        self.key_func = key_func or self._default_key
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = time.monotonic()

    @staticmethod
    def _default_key(request: Request) -> str:
        # Prefer the authenticated subject so one abusive user cannot exhaust
        # the budget for everyone behind the same NAT/proxy address.
        user = getattr(request.state, "user", None)
        if isinstance(user, dict) and user.get("sub"):
            return f"sub:{user['sub']}"
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"
        return f"ip:{request.client.host if request.client else 'unknown'}"

    def _sweep(self, now: float) -> None:
        """Drop idle buckets so the map cannot grow without bound."""
        if now - self._last_sweep < self.window:
            return
        cutoff = now - self.window
        for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
            self._hits.pop(key, None)
        self._last_sweep = now

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self.exempt_paths:
            return await call_next(request)

        now = time.monotonic()
        self._sweep(now)

        key = self.key_func(request)
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= self.limit:
            retry_after = max(1, int(self.window - (now - bucket[0])))
            return JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        bucket.append(now)
        response = await call_next(request)
        response.headers.setdefault("X-RateLimit-Limit", str(self.limit))
        response.headers.setdefault(
            "X-RateLimit-Remaining", str(max(0, self.limit - len(bucket)))
        )
        return response
