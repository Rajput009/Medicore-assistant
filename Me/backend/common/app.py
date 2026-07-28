"""Shared FastAPI application factory for production-safe defaults.

Every MediCore service goes through ``create_service_app`` so security
controls cannot drift between gateway, auth, patient-flow and CDS:

* OpenAPI /docs /redoc are **off** outside local/test (they advertise every
  route, model and error shape to unauthenticated callers).
* TrustedHostMiddleware rejects Host-header attacks when configured.
* CORS is an explicit allow-list (never ``*`` with credentials).
* Security headers, body-size limits, rate limits and the audit trail are
  applied in a fixed, documented order.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.common.config import Settings
from backend.common.config import settings as default_settings
from backend.common.csrf import CookieCSRFMiddleware
from backend.common.errors import install_error_handlers
from backend.common.hardening import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from backend.common.logging import configure_logging
from backend.common.middleware import AuditLogMiddleware
from backend.common.telemetry import instrument_fastapi


def create_service_app(
    *,
    title: str,
    service_name: str,
    version: str = "1.0.0",
    cfg: Settings | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[Any]] | None = None,
    rate_limit: int | None = None,
    enable_cors: bool = False,
    cors_methods: Sequence[str] = ("GET", "POST", "OPTIONS"),
    cors_headers: Sequence[str] = (
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-CSRF-Token",
    ),
    extra_middleware: Iterable[tuple[type, dict[str, Any]]] = (),
) -> FastAPI:
    """Build a FastAPI app with the standard MediCore hardening stack.

    Middleware registration order (last added = outermost):

      1. JWT / service-specific auth          (innermost, optional)
      2. RateLimitMiddleware
      3. BodySizeLimitMiddleware
      4. AuditLogMiddleware
      5. SecurityHeadersMiddleware
      6. CORSMiddleware                       (when enable_cors)
      7. TrustedHostMiddleware                (outermost when configured)
    """
    cfg = cfg or default_settings
    configure_logging(cfg.log_level, service=service_name)

    # Disable interactive docs outside local/test. The OpenAPI document
    # enumerates every PHI-bearing route and is a free reconnaissance map.
    docs_url = "/docs" if cfg.expose_api_docs else None
    redoc_url = "/redoc" if cfg.expose_api_docs else None
    openapi_url = "/openapi.json" if cfg.expose_api_docs else None

    app = FastAPI(
        title=title,
        version=version,
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )
    app = instrument_fastapi(app, service_name=service_name)

    # Service-specific middleware first (innermost).
    for cls, kwargs in extra_middleware:
        app.add_middleware(cls, **kwargs)

    # CSRF sits just outside auth so cookie-only unsafe requests are rejected
    # before they hit handlers, while Bearer-authenticated calls pass through.
    app.add_middleware(CookieCSRFMiddleware)

    limit = rate_limit if rate_limit is not None else cfg.rate_limit_per_minute
    app.add_middleware(RateLimitMiddleware, limit=limit)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=cfg.max_request_body_bytes)
    app.add_middleware(AuditLogMiddleware, service=service_name)
    app.add_middleware(SecurityHeadersMiddleware, hsts=cfg.enable_hsts)

    if enable_cors:
        origins = cfg.cors_origins
        # Never fall back to "*" — credentialed CORS with a wildcard is a
        # browser-enforced footgun we refuse to enable.
        if origins and "*" not in origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=list(origins),
                allow_credentials=True,
                allow_methods=list(cors_methods),
                allow_headers=list(cors_headers),
                max_age=600,
            )

    hosts = cfg.trusted_hosts_list
    if hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    install_error_handlers(app)
    return app


def empty_lifespan() -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """No-op lifespan for services that hold no process-wide state."""

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    return _lifespan
