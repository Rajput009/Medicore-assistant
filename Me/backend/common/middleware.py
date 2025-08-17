from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
import json, time

class AuditLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration_ms = int((time.time()-start)*1000)
        # Minimal structured log; redact obvious PHI fields if present
        log = {
            "path": request.url.path,
            "method": request.method,
            "status": response.status_code,
            "dur_ms": duration_ms
        }
        print(json.dumps(log))
        return response
