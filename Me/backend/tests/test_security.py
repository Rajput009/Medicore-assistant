"""Tests for token minting/verification."""

import base64
import json
import time

import pytest
from jose import JWTError, jwt

from backend.common.config import settings
from backend.common.security import create_access_token, verify_access_token


def test_round_trip():
    token = create_access_token("alice", roles=["clinician"])
    claims = verify_access_token(token)
    assert claims["sub"] == "alice"
    assert claims["roles"] == ["clinician"]
    assert claims.get("token_use") == "access"


def test_default_ttl_is_short_lived():
    """Stolen tokens must stop working quickly; default is 15 minutes."""
    token = create_access_token("alice")
    claims = verify_access_token(token)
    remaining = claims["exp"] - int(time.time())
    assert remaining <= settings.access_token_ttl_minutes * 60 + 5
    assert remaining > 0


def test_non_access_token_use_is_rejected():
    """A future refresh token must not be accepted as an access token."""
    token = jwt.encode(
        {
            "sub": "alice",
            "roles": ["admin"],
            "exp": int(time.time()) + 600,
            "token_use": "refresh",
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(JWTError):
        verify_access_token(token)


def test_default_role_is_least_privileged():
    claims = verify_access_token(create_access_token("bob"))
    assert claims["roles"] == ["viewer"]


def test_expired_token_rejected():
    token = create_access_token("alice", expires_minutes=-1)
    with pytest.raises(JWTError):
        verify_access_token(token)


def test_token_without_sub_rejected():
    token = jwt.encode(
        {"roles": ["admin"], "exp": int(time.time()) + 600},
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(JWTError):
        verify_access_token(token)


def _unsigned(header: dict, payload: dict, signature: str = "") -> str:
    """Hand-craft a token so we can forge headers the signing lib won't emit."""

    def b64(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64(header)}.{b64(payload)}.{signature}"


def test_alg_none_rejected():
    """A token with alg=none must never be accepted."""
    token = _unsigned({"alg": "none", "typ": "JWT"}, {"sub": "mallory"})
    with pytest.raises(JWTError):
        verify_access_token(token)


def test_wrong_secret_rejected():
    token = jwt.encode({"sub": "mallory"}, "not-the-secret", algorithm="HS256")
    with pytest.raises(JWTError):
        verify_access_token(token)


def test_rs256_without_jwks_rejected():
    """RS256 tokens must not fall through to the HS (shared secret) path."""
    forged = _unsigned({"alg": "RS256", "typ": "JWT"}, {"sub": "mallory"}, "sig")
    with pytest.raises(JWTError):
        verify_access_token(forged)


@pytest.mark.parametrize("bad", ["", "   ", "not-a-jwt", "a.b"])
def test_malformed_tokens_rejected(bad):
    with pytest.raises(JWTError):
        verify_access_token(bad)
