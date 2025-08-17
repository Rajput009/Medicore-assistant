from datetime import datetime, timedelta, timezone
from jose import jwt
from typing import Dict, Any
from .config import settings

def create_access_token(sub: str, roles: list[str] | None = None, expires_minutes: int = 60) -> str:
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": sub,
        "roles": roles or ["viewer"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


from jose import JWTError, jwt
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from backend.common.config import settings

# def verify_access_token_OLD(token: str) -> Dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
        # basic expiry check handled by jwt library; but ensure 'sub' exists
        sub = payload.get("sub")
        if not sub:
            raise JWTError("sub claim missing")
        return payload
    except JWTError as e:
        raise


from jose import JWTError, jwt
from typing import Dict, Any
from datetime import datetime, timezone
from backend.common.config import settings
from backend.common.jwks import JWKSFetcher
import os

_jwks_fetcher: JWKSFetcher | None = None

def _get_jwks_fetcher() -> JWKSFetcher | None:
    global _jwks_fetcher
    jwks_url = os.getenv("OIDC_JWKS_URI") or os.getenv("OIDC_ISSUER")
    # If issuer provided, try to construct .well-known/jwks.json
    if jwks_url and jwks_url.startswith("http") and "jwks" not in jwks_url:
        if jwks_url.endswith("/.well-known/openid-configuration"):
            # fetch config? but we'll expect OIDC_JWKS_URI to be set in prod. Simple fallback:
            jwks_url = jwks_url.rsplit("/", 1)[0] + "/jwks"
    if not jwks_url:
        return None
    if _jwks_fetcher is None:
        _jwks_fetcher = JWKSFetcher(jwks_url, ttl=600)
    return _jwks_fetcher

def verify_access_token(token: str) -> Dict[str, Any]:
    # Try decode with configured algorithm (HS or RS). If RS, fetch JWKS and find matching key.
    try:
        header = jwt.get_unverified_header(token)
        alg = header.get("alg", "HS256")
    except Exception as e:
        raise JWTError("invalid token header") from e

    # HS family
    if alg.startswith("HS"):
        try:
            payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
            sub = payload.get("sub")
            if not sub:
                raise JWTError("sub claim missing")
            return payload
        except JWTError as e:
            raise
    # RS family: use JWKS
    elif alg.startswith("RS") or alg.startswith("ES"):
        fetcher = _get_jwks_fetcher()
        if not fetcher:
            raise JWTError("no JWKS configured for RS/ES token verification")
        keys = fetcher.get_keys()
        kid = header.get("kid")
        key = None
        if kid:
            for k in keys:
                if k.get("kid") == kid:
                    key = k
                    break
        else:
            # fallback: take first key
            if keys:
                key = keys[0]
        if not key:
            raise JWTError("matching JWK not found")
        try:
            payload = jwt.decode(token, key, algorithms=[alg], options={"verify_aud": False})
            sub = payload.get("sub")
            if not sub:
                raise JWTError("sub claim missing")
            return payload
        except JWTError as e:
            raise
    else:
        raise JWTError("unsupported alg")
