# Build context is the "backend/" directory so the shared "backend.common"
# package is available inside the image:
#   docker build -f services/patient_flow/Dockerfile -t medicore/patient_flow .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install deps first so the layer is cached across source changes.
COPY services/patient_flow/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Shared library + this service, importable as "backend.*".
COPY __init__.py /app/backend/__init__.py
COPY common /app/backend/common
COPY services/__init__.py /app/backend/services/__init__.py
COPY services/patient_flow /app/backend/services/patient_flow

# Drop root privileges.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8082/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "backend.services.patient_flow.main:app", "--host", "0.0.0.0", "--port", "8082"]
