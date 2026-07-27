"""JWT minting and verification helpers.

Supports:
  * HS* tokens signed with the shared ``JWT_SECRET`` (local/dev + internal tokens)
  * RS*/ES* tokens issued by an external IdP, verified against its JWKS.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt

from .config import settings
from .jwks import JWKSFetcher

# Algorithms we are willing to verify. Anything else (notably "none") is rejected
# outright so a caller cannot downgrade the algorithm via the token header.
_ALLOWED_HS = {"HS256", "HS384", "HS512"}
_ALLOWED_ASYMMETRIC = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}


def create_access_token(
    sub: str,
    roles: list[str] | None = None,
    expires_minutes: int | None = None,
) -> str:
    """Mint an internal HS-signed access token.

    Default lifetime comes from ``settings.access_token_ttl_minutes`` (15m) so a
    stolen token stops working quickly. Callers that need a different window
    (tests, short-lived bootstrap) pass ``expires_minutes`` explicitly.
    """
    ttl = (
        settings.access_token_ttl_minutes
        if expires_minutes is None
        else expires_minutes
    )
    if ttl <= 0:
        # Negative/zero TTL is only useful in tests to force expiry; clamp the
        # resulting exp slightly into the past so verification always fails.
        delta = timedelta(seconds=ttl * 60 if ttl < 0 else -1)
    else:
        delta = timedelta(minutes=ttl)
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": sub,
        "roles": roles or ["viewer"],
        "iat": int(now.timestamp()),
        "exp": int((now + delta).timestamp()),
        # Explicit token type so a future refresh token cannot be replayed as
        # an access token if we ever issue both from the same secret.
        "token_use": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


_jwks_fetcher: JWKSFetcher | None = None


def _resolve_jwks_url() -> str | None:
    """Work out the JWKS endpoint from the configured OIDC environment."""
    jwks_url = settings.oidc_jwks_uri or settings.oidc_issuer
    if not jwks_url:
        return None
    jwks_url = jwks_url.strip()
    if not jwks_url.startswith("http"):
        return None
    if "jwks" in jwks_url:
        return jwks_url
    # Derive the JWKS endpoint from a discovery document / bare issuer URL.
    if jwks_url.endswith("/.well-known/openid-configuration"):
        base = jwks_url[: -len("/.well-known/openid-configuration")]
    else:
        base = jwks_url.rstrip("/")
    return f"{base}/.well-known/jwks.json"


def _get_jwks_fetcher() -> JWKSFetcher | None:
    global _jwks_fetcher
    jwks_url = _resolve_jwks_url()
    if not jwks_url:
        return None
    # Rebuild the fetcher if the configured URL changed (e.g. in tests).
    if _jwks_fetcher is None or _jwks_fetcher.url != jwks_url:
        _jwks_fetcher = JWKSFetcher(jwks_url, ttl=600)
    return _jwks_fetcher


def reset_jwks_cache() -> None:
    """Drop the memoised JWKS fetcher (useful for tests / key rotation)."""
    global _jwks_fetcher
    _jwks_fetcher = None


def _decode_options() -> dict[str, Any]:
    # ``aud``/``iss`` are only validated when explicitly configured.
    return {
        "verify_aud": bool(settings.oidc_audience),
        "verify_iss": bool(settings.oidc_issuer_claim),
    }


def _finalise(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload.get("sub"):
        raise JWTError("sub claim missing")
    # Reject tokens that advertise a non-access purpose (e.g. a future refresh
    # token minted under the same secret). Tokens without the claim remain
    # accepted for backwards compatibility with already-issued sessions.
    token_use = payload.get("token_use")
    if token_use is not None and token_use != "access":
        raise JWTError("token is not an access token")
    return payload


def verify_access_token(token: str) -> dict[str, Any]:
    """Verify ``token`` and return its claims, raising ``JWTError`` when invalid."""
    if not token or not token.strip():
        raise JWTError("empty token")

    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:  # malformed token
        raise JWTError("invalid token header") from exc

    alg = header.get("alg")
    if not alg:
        raise JWTError("missing alg header")

    if alg in _ALLOWED_HS:
        # Pin verification to the *configured* algorithm so a token cannot pick
        # a weaker HS variant than the deployment expects.
        if settings.jwt_alg not in _ALLOWED_HS:
            raise JWTError("server is not configured for HS tokens")
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_alg],
            audience=settings.oidc_audience or None,
            issuer=settings.oidc_issuer_claim or None,
            options=_decode_options(),
        )
        return _finalise(payload)

    if alg in _ALLOWED_ASYMMETRIC:
        fetcher = _get_jwks_fetcher()
        if not fetcher:
            raise JWTError("no JWKS configured for RS/ES token verification")

        kid = header.get("kid")
        key = _select_key(fetcher.get_keys(), kid)
        if key is None and kid:
            # Key may have been rotated since we last cached the JWKS.
            key = _select_key(fetcher.refresh(), kid)
        if key is None:
            raise JWTError("matching JWK not found")

        payload = jwt.decode(
            token,
            key,
            algorithms=[alg],
            audience=settings.oidc_audience or None,
            issuer=settings.oidc_issuer_claim or None,
            options=_decode_options(),
        )
        return _finalise(payload)

    raise JWTError(f"unsupported alg: {alg}")


def _select_key(keys: list[dict[str, Any]], kid: str | None) -> dict[str, Any] | None:
    if not keys:
        return None
    if kid:
        for k in keys:
            if k.get("kid") == kid:
                return k
        return None
    # No kid in the header: only safe to guess when the IdP publishes one key.
    return keys[0] if len(keys) == 1 else None
