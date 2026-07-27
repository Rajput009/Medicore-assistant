"""CSRF defence for cookie-authenticated mutations."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from backend.common.csrf import (
    CSRF_COOKIE,
    CSRF_HEADER,
    CookieCSRFMiddleware,
    issue_csrf_cookie,
)
from backend.common.security import create_access_token


@pytest.fixture()
def app(monkeypatch):
    from backend.common import config

    # Origin allow-list used by the CSRF check.
    monkeypatch.setattr(
        config.settings, "allowed_origins", "https://console.medicore.local"
    )
    monkeypatch.setattr(config.settings, "auth_cookie_name", "medicore_session")

    application = FastAPI()
    application.add_middleware(CookieCSRFMiddleware)

    @application.post("/mutate")
    def mutate():
        return {"ok": True}

    @application.get("/read")
    def read():
        return {"ok": True}

    @application.post("/login")
    def login():
        return {"ok": True}

    return application


def _cookie_client(app, token: str | None = None) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    if token is not None:
        client.cookies.set("medicore_session", token)
    return client


class TestCookieCSRF:
    def test_safe_methods_are_never_blocked(self, app):
        token = create_access_token("u", roles=["clinician"])
        client = _cookie_client(app, token)
        assert client.get("/read").status_code == 200

    def test_bearer_auth_bypasses_csrf(self, app):
        token = create_access_token("u", roles=["clinician"])
        client = _cookie_client(app)  # no cookie
        r = client.post(
            "/mutate", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200

    def test_anonymous_post_is_not_csrf_blocked(self, app):
        """/login must work before any session cookie exists."""
        client = _cookie_client(app)
        assert client.post("/login").status_code == 200

    def test_cookie_only_post_without_origin_is_rejected(self, app):
        token = create_access_token("u", roles=["clinician"])
        client = _cookie_client(app, token)
        r = client.post("/mutate")
        assert r.status_code == 403
        assert "CSRF" in r.json()["detail"]

    def test_cookie_only_post_with_allowed_origin_passes(self, app):
        token = create_access_token("u", roles=["clinician"])
        client = _cookie_client(app, token)
        r = client.post(
            "/mutate", headers={"Origin": "https://console.medicore.local"}
        )
        assert r.status_code == 200

    def test_disallowed_origin_is_rejected(self, app):
        token = create_access_token("u", roles=["clinician"])
        client = _cookie_client(app, token)
        r = client.post(
            "/mutate", headers={"Origin": "https://evil.example"}
        )
        assert r.status_code == 403

    def test_double_submit_csrf_header_passes(self, app):
        token = create_access_token("u", roles=["clinician"])
        client = _cookie_client(app, token)
        # Mint a CSRF cookie the way login does.
        from starlette.responses import Response

        resp = Response()
        csrf = issue_csrf_cookie(resp, secure=False)
        client.cookies.set(CSRF_COOKIE, csrf)
        r = client.post("/mutate", headers={CSRF_HEADER: csrf})
        assert r.status_code == 200

    def test_mismatched_csrf_header_is_rejected(self, app):
        token = create_access_token("u", roles=["clinician"])
        client = _cookie_client(app, token)
        client.cookies.set(CSRF_COOKIE, "aaa")
        r = client.post("/mutate", headers={CSRF_HEADER: "bbb"})
        assert r.status_code == 403

    def test_referer_from_allowed_origin_passes(self, app):
        token = create_access_token("u", roles=["clinician"])
        client = _cookie_client(app, token)
        r = client.post(
            "/mutate",
            headers={"Referer": "https://console.medicore.local/beds"},
        )
        assert r.status_code == 200
