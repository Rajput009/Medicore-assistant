"""Token revocation (denylist) and auth logout/session endpoints."""

from __future__ import annotations

import pytest
from jose import JWTError, jwt
from starlette.testclient import TestClient

from backend.common.config import settings
from backend.common.revocation import is_revoked, reset_revocation_store, revoke_payload
from backend.common.security import create_access_token, verify_access_token


@pytest.fixture(autouse=True)
def _clean_denylist():
    reset_revocation_store()
    yield
    reset_revocation_store()


class TestRevocationStore:
    def test_minted_token_carries_jti(self):
        claims = verify_access_token(create_access_token("alice", roles=["clinician"]))
        assert claims.get("jti")
        assert len(str(claims["jti"])) >= 16

    def test_revoked_token_is_rejected(self):
        token = create_access_token("alice", roles=["clinician"])
        claims = verify_access_token(token)
        assert revoke_payload(claims) is True
        assert is_revoked(str(claims["jti"])) is True
        with pytest.raises(JWTError, match="revoked"):
            verify_access_token(token)

    def test_unrelated_token_still_valid(self):
        a = create_access_token("alice")
        b = create_access_token("bob")
        revoke_payload(verify_access_token(a))
        assert verify_access_token(b)["sub"] == "bob"

    def test_payload_without_jti_cannot_be_revoked(self):
        # Hand-crafted legacy token (no jti) — revoke is a no-op, verify still works.
        token = jwt.encode(
            {
                "sub": "legacy",
                "roles": ["viewer"],
                "exp": __import__("time").time() + 600,
                "token_use": "access",
            },
            settings.jwt_secret,
            algorithm="HS256",
        )
        claims = verify_access_token(token)
        assert revoke_payload(claims) is False
        assert verify_access_token(token)["sub"] == "legacy"


class TestAuthLogoutAndSession:
    @pytest.fixture()
    def client(self):
        import backend.services.auth.main as auth

        with TestClient(auth.app, raise_server_exceptions=False) as c:
            yield c

    @staticmethod
    def _ip(n: int) -> dict[str, str]:
        # Auth rate-limits at 10/min per caller. Unique XFF keeps each test
        # off every other test's budget.
        return {"X-Forwarded-For": f"203.0.113.{n}"}

    def test_login_sets_httponly_cookie(self, client):
        r = client.post(
            "/login",
            json={"username": "dr.smith", "password": "medicore-dev"},
            headers=self._ip(1),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["access_token"]
        assert body["expires_in"] <= 15 * 60
        # httpOnly session cookie for the SPA.
        assert settings.auth_cookie_name in r.cookies

    def test_session_endpoint_returns_claims(self, client):
        headers = self._ip(2)
        login = client.post(
            "/login",
            json={"username": "dr.smith", "password": "medicore-dev"},
            headers=headers,
        )
        token = login.json()["access_token"]
        r = client.get(
            "/session",
            headers={**headers, "Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["sub"] == "dr.smith"
        assert "clinician" in r.json()["roles"]
        # Raw token must never be echoed back.
        assert "access_token" not in r.json()

    def test_session_via_cookie_without_bearer(self, client):
        headers = self._ip(3)
        client.post(
            "/login",
            json={"username": "dr.smith", "password": "medicore-dev"},
            headers=headers,
        )
        # TestClient keeps cookies automatically.
        r = client.get("/session", headers=headers)
        assert r.status_code == 200
        assert r.json()["sub"] == "dr.smith"

    def test_logout_revokes_token_and_clears_cookie(self, client):
        headers = self._ip(4)
        login = client.post(
            "/login",
            json={"username": "dr.smith", "password": "medicore-dev"},
            headers=headers,
        )
        token = login.json()["access_token"]
        auth_h = {**headers, "Authorization": f"Bearer {token}"}
        assert client.get("/session", headers=auth_h).status_code == 200

        out = client.post("/logout", headers=auth_h)
        assert out.status_code == 200
        assert out.json()["status"] == "ok"

        # Token is dead.
        assert client.get("/session", headers=auth_h).status_code == 401
        with pytest.raises(JWTError):
            verify_access_token(token)

    def test_logout_without_token_still_succeeds(self, client):
        r = client.post("/logout", headers=self._ip(5))
        assert r.status_code == 200


class TestGatewayAcceptsCookie:
    @pytest.fixture()
    def gateway(self, monkeypatch):
        import backend.services.gateway.main as gw

        async def fake_read(resource, resource_id):
            return {"resourceType": resource, "id": resource_id}

        async def nothing(*a, **k):
            return None

        monkeypatch.setattr(gw.fhir, "read", fake_read)
        monkeypatch.setattr(gw, "get_cached", nothing)
        monkeypatch.setattr(gw, "set_cached", nothing)
        with TestClient(gw.app, raise_server_exceptions=False) as c:
            yield c

    def test_cookie_authenticates_fhir_read(self, gateway):
        token = create_access_token("cookie-user", roles=["clinician"])
        gateway.cookies.set(settings.auth_cookie_name, token)
        r = gateway.get("/fhir/patient/abc")
        assert r.status_code == 200
        assert r.json()["id"] == "abc"

    def test_revoked_cookie_is_rejected(self, gateway):
        token = create_access_token("cookie-user", roles=["clinician"])
        revoke_payload(verify_access_token(token))
        gateway.cookies.set(settings.auth_cookie_name, token)
        assert gateway.get("/fhir/patient/abc").status_code == 401


class TestEstablishSession:
    @pytest.fixture()
    def client(self):
        import backend.services.auth.main as auth

        with TestClient(auth.app, raise_server_exceptions=False) as c:
            yield c

    @staticmethod
    def _ip(n: int) -> dict[str, str]:
        return {"X-Forwarded-For": f"203.0.113.{n}"}

    def test_establish_session_sets_cookie(self, client):
        headers = self._ip(20)
        token = create_access_token("oidc.user", roles=["clinician"])
        r = client.post(
            "/session/establish",
            json={"access_token": token},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["sub"] == "oidc.user"
        assert "access_token" not in r.json()
        s = client.get("/session", headers=headers)
        assert s.status_code == 200
        assert s.json()["sub"] == "oidc.user"

    def test_establish_rejects_garbage(self, client):
        r = client.post(
            "/session/establish",
            json={"access_token": "not-a-valid-jwt-token-value-xx"},
            headers=self._ip(21),
        )
        assert r.status_code == 401
