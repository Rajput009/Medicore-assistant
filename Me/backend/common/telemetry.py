import os
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor

SERVICE_NAMESPACE = os.getenv("OTEL_SERVICE_NAMESPACE", "medicore")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

def init_otel(service_name: str):
    resource = Resource.create({
        "service.name": service_name,
        "service.namespace": SERVICE_NAMESPACE
    })
    provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(provider)
    exporter = OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    LoggingInstrumentor().instrument()
    RequestsInstrumentor().instrument()

def instrument_fastapi(app, service_name: str):
    init_otel(service_name)
    FastAPIInstrumentor.instrument_app(app)
    return app
