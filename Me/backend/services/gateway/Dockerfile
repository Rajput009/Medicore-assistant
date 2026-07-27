# Build context is the "backend/" directory so the shared "backend.common"
# package is available inside the image:
#   docker build -f services/gateway/Dockerfile -t medicore/gateway .
#
# Pin the base digest in your registry mirror for air-gapped / supply-chain
# controlled environments. The tag tracks upstream security patches.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    # Fail closed if a dependency tries to write outside /tmp.
    TMPDIR=/tmp

WORKDIR /app

# Install deps first so the layer is cached across source changes.
COPY services/gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && rm -rf /root/.cache/pip

# Shared library + this service, importable as "backend.*".
COPY __init__.py /app/backend/__init__.py
COPY common /app/backend/common
COPY services/__init__.py /app/backend/services/__init__.py
COPY services/gateway /app/backend/services/gateway

# Drop root privileges. Fixed uid matches the k8s securityContext.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# /ready is intentionally unauthenticated (probes send no Authorization).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=4).status==200 else 1)"

# Single worker: the process holds a Postgres pool and a janitor task that are
# not safe to share across forked workers. Scale out with replicas, not --workers.
CMD ["uvicorn", "backend.services.gateway.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-access-log", "--timeout-keep-alive", "15"]
