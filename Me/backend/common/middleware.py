"""Structured audit logging.

HIPAA-aware: the log line must never contain PHI. FHIR query strings routinely
carry identifiers (``?patient=123&birthdate=...``), so parameter *values* are
redacted and only the parameter names are recorded.
"""

import json
import logging
import sys
import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("medicore.audit")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

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
