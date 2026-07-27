"""Production-safe error responses.

Internal exception messages must never reach the client: they can contain
hostnames, DSNs, driver stack fragments, or PHI. Handlers log the real error
and return a stable, non-sensitive ``detail`` string.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.common.config import settings

logger = logging.getLogger("medicore.errors")

# Generic messages — never interpolate exception text into these.
_GENERIC_500 = "An unexpected error occurred"
_GENERIC_502 = "Upstream service unavailable"
_GENERIC_503 = "Service temporarily unavailable"
_GENERIC_400 = "Invalid request"


def public_detail(status_code: int, *, preferred: str | None = None) -> str:
    """Choose a client-safe detail string for ``status_code``."""
    if preferred and not settings.is_production:
        return preferred
    if status_code == 400:
        return preferred or _GENERIC_400
    if status_code == 401:
        return preferred or "Not authenticated"
    if status_code == 403:
        return preferred or "Forbidden"
    if status_code == 404:
        return preferred or "Not found"
    if status_code == 409:
        return preferred or "Conflict"
    if status_code == 413:
        return preferred or "Request body too large"
    if status_code == 422:
        return preferred or "Validation failed"
    if status_code == 429:
        return preferred or "Rate limit exceeded"
    if status_code == 502:
        return _GENERIC_502
    if status_code == 503:
        return _GENERIC_503
    if status_code >= 500:
        return _GENERIC_500
    return preferred or _GENERIC_400


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers that scrub error bodies in production."""

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Validation errors are safe (field paths + messages from our schemas).
        # Still avoid dumping full input bodies which may contain PHI/passwords.
        errors = []
        for err in exc.errors():
            errors.append(
                {
                    "loc": err.get("loc"),
                    "msg": err.get("msg"),
                    "type": err.get("type"),
                }
            )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": errors if not settings.is_production else "Validation failed"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, str):
            safe = public_detail(exc.status_code, preferred=detail)
        else:
            # Structured details (e.g. list) — only in non-prod.
            safe = detail if not settings.is_production else public_detail(exc.status_code)
        headers = getattr(exc, "headers", None) or {}
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": safe},
            headers=dict(headers),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled error",
            extra={
                "path": request.url.path,
                "method": request.method,
                "error_type": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": public_detail(500)},
        )


def http_error(
    status_code: int,
    detail: str,
    *,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """Build an HTTPException with a production-safe detail."""
    return HTTPException(
        status_code=status_code,
        detail=public_detail(status_code, preferred=detail),
        headers=headers,
    )
