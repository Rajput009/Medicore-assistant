"""Structured audit logging.

HIPAA-aware: the log line must never contain PHI. FHIR query strings routinely
carry identifiers (``?patient=123&birthdate=...``), so parameter *values* are
redacted and only the parameter names are recorded.
"""

import json
import logging
import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Emitted through the root logger configured by backend.common.logging so the
# audit trail lands in the same JSON stream as everything else.
logger = logging.getLogger("medicore.audit")

# Path segments that may contain a patient/resource identifier.
_ID_BEARING_PREFIXES = ("/fhir/", "/cache/")


def _safe_path(path: str) -> str:
    """Replace identifier path segments with a placeholder."""
    if not path.startswith(_ID_BEARING_PREFIXES):
        return path
    parts = path.split("/")
    # /fhir/<type>/<id> -> /fhir/<type>/{id}
    if len(parts) >= 4 and parts[3] and parts[3] != "search":
        parts[3] = "{id}"
    return "/".join(parts)


class AuditLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        # Correlate log lines with the client's request id when supplied.
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id

        log: dict[str, Any] = {
            "request_id": request_id,
            "path": _safe_path(request.url.path),
            "method": request.method,
            # Names only — values may be PHI.
            "query_keys": sorted(request.query_params.keys()),
            "client": request.client.host if request.client else None,
        }

        try:
            response = await call_next(request)
        except Exception as exc:
            # Still emit an audit record when the handler blows up.
            log.update(
                {
                    "status": 500,
                    "dur_ms": int((time.perf_counter() - start) * 1000),
                    "error": type(exc).__name__,
                }
            )
            logger.info(json.dumps(log))
            raise

        user = getattr(request.state, "user", None)
        if isinstance(user, dict) and user.get("sub"):
            log["sub"] = user["sub"]

        log.update(
            {
                "status": response.status_code,
                "dur_ms": int((time.perf_counter() - start) * 1000),
            }
        )
        logger.info(json.dumps(log))
        response.headers["x-request-id"] = request_id
        return response


def redact_phi_loggers() -> None:
    """Raise third-party HTTP client loggers above INFO.

    httpx (and httpcore/urllib3) log the full request URL at INFO. Because
    MediCore URLs embed patient identifiers, leaving them enabled writes PHI
    into the log stream. Importing this module applies the setting so it holds
    even when configure_logging has not run (e.g. under pytest).
    """
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


redact_phi_loggers()
