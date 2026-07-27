# Build context is the "backend/" directory:
#   docker build -f services/cds/Dockerfile -t medicore/cds .
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TMPDIR=/tmp

WORKDIR /app

COPY services/cds/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && rm -rf /root/.cache/pip

COPY __init__.py /app/backend/__init__.py
COPY common /app/backend/common
COPY services/__init__.py /app/backend/services/__init__.py
COPY services/cds /app/backend/services/cds

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8083

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8083/ready', timeout=4).status==200 else 1)"

CMD ["uvicorn", "backend.services.cds.main:app", \
     "--host", "0.0.0.0", "--port", "8083", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-access-log", "--timeout-keep-alive", "15"]
