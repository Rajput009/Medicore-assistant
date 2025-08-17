from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request, HTTPException, status
from backend.common.security import verify_access_token
from typing import List

EXEMPT_PATHS = ["/health", "/docs", "/openapi.json", "/favicon.ico", "/metrics"]

class JWTAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # allow exempt paths (health, static, openapi)
        for p in EXEMPT_PATHS:
            if path.startswith(p):
                return await call_next(request)
        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        if not auth or not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing auth header")
        token = auth.split(" ", 1)[1]
        try:
            payload = verify_access_token(token)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token invalid or expired")
        # attach user info to request.state for downstream handlers
        request.state.user = {
            "sub": payload.get("sub"),
            "roles": payload.get("roles", []),
            "claims": payload
        }
        return await call_next(request)
