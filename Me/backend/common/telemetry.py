"""OpenTelemetry setup.

Instrumentation must be idempotent: several service modules import this and the
SDK raises/warns if a provider or instrumentor is registered twice.
"""

import logging
import os

logger = logging.getLogger(__name__)

SERVICE_NAMESPACE = os.getenv("OTEL_SERVICE_NAMESPACE", "medicore")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
OTEL_ENABLED = os.getenv("OTEL_ENABLED", "true").lower() not in ("0", "false", "no")

_initialised = False


def init_otel(service_name: str) -> bool:
    """Configure the tracer provider once per process. Returns True if tracing is on."""
    global _initialised
    if not OTEL_ENABLED:
        return False
    if _initialised:
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        # Telemetry is optional; the service must still start without it.
        logger.warning("OpenTelemetry not installed, tracing disabled: %s", exc)
        return False

    try:
        resource = Resource.create(
            {"service.name": service_name, "service.namespace": SERVICE_NAMESPACE}
        )
        provider = TracerProvider(resource=resource)
        # The exporter wants the full signal path.
        endpoint = OTLP_ENDPOINT.rstrip("/")
        if not endpoint.endswith("/v1/traces"):
            endpoint = f"{endpoint}/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)

        LoggingInstrumentor().instrument(set_logging_format=False)

        # The services use httpx, not requests, so instrument httpx when present.
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        except ImportError:
            pass

        _initialised = True
        return True
    except Exception as exc:
        logger.warning("Failed to initialise OpenTelemetry, continuing without: %s", exc)
        return False


def instrument_fastapi(app, service_name: str):
    """Instrument a FastAPI app; always returns the app, even if tracing fails."""
    if not init_otel(service_name):
        return app
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # Health checks are noisy and carry no useful trace signal.
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,healthz,metrics")
    except Exception as exc:
        logger.warning("FastAPI instrumentation failed: %s", exc)
    return app
