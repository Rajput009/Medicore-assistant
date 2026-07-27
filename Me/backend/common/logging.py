"""Structured JSON logging.

Log aggregators (CloudWatch, Loki, Datadog) parse JSON; plain text forces
brittle regex parsing and loses fields. Every record carries the service name
and, when tracing is active, the trace/span ids so a log line can be correlated
with a distributed trace.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes present on every LogRecord; anything else was supplied by the
# caller via `extra=` and should be forwarded to the aggregator.
_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # OpenTelemetry's LoggingInstrumentor injects these when a span is active.
        for attr in ("otelTraceID", "otelSpanID"):
            value = getattr(record, attr, None)
            if value and value != "0" * len(str(value)):
                payload[attr.replace("otel", "").lower()] = value

        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_") or key in payload:
                continue
            if key in ("otelTraceID", "otelSpanID", "otelServiceName"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", service: str = "medicore") -> None:
    """Install JSON logging on the root logger. Idempotent."""
    resolved = getattr(logging, str(level).upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved)

    # Uvicorn installs its own handlers; let records propagate to root instead
    # so everything is emitted in one consistent format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        target.handlers.clear()
        target.propagate = True

    # Access logs duplicate the audit middleware, which is richer and redacted.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # PHI protection: these libraries log full request URLs at INFO, and our
    # URLs carry patient identifiers (e.g. /fhir/patient/MRN-123?birthdate=...).
    # Raising them to WARNING keeps identifiers out of the log stream; the
    # audit middleware already records every request with values redacted.
    for noisy in ("httpx", "httpcore", "urllib3", "pymongo", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
