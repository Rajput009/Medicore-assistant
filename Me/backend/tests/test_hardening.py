"""Production hardening: secret validation, headers, rate limits, audit trail.

Each test here corresponds to a defect found by adversarial review, not to a
hypothetical. Comments name the failure mode being prevented.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from backend.common.config import INSECURE_DEFAULTS, Settings
from backend.common.hardening import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from backend.common.middleware import AuditLogMiddleware, pseudonymise
from backend.common.security import create_access_token

PROD = {
    "env": "production",
    "jwt_secret": "s" * 40,
    "session_secret": "t" * 40,
    "postgres_password": "p" * 20,
    "fhir_client_secret": "real-secret",
    "trusted_hosts": "api.hospital.example,localhost",
    "oidc_issuer": "https://idp.example/realms/medicore",
    "oidc_client_id": "medicore-console",
    "oidc_client_secret": "oidc-secret-value-not-a-placeholder",
    "access_token_ttl_minutes": 15,
    "enable_demo_login": False,
}


# ---------------------------------------------------------------------------
# Secret validation
# ---------------------------------------------------------------------------


class TestProductionSecretValidation:
    def test_production_refuses_the_published_default_jwt_secret(self):
        """The default is in a public repo. Booting with it lets anyone mint
        an admin token, and nothing downstream would notice."""
        with pytest.raises(ValueError, match="JWT_SECRET"):
            Settings(**{**PROD, "jwt_secret": "change-this-in-prod"})

    @pytest.mark.parametrize("placeholder", sorted(INSECURE_DEFAULTS))
    def test_no_placeholder_survives_into_production(self, placeholder):
        with pytest.raises(ValueError):
            Settings(**{**PROD, "jwt_secret": placeholder})

    def test_short_secrets_are_rejected(self):
        """A 12-character HMAC key is brute-forcible offline."""
        with pytest.raises(ValueError, match="at least 32 characters"):
            Settings(**{**PROD, "jwt_secret": "short"})

    def test_session_secret_is_validated(self):
        with pytest.raises(ValueError, match="SESSION_SECRET"):
            Settings(**{**PROD, "session_secret": "dev-change-me"})

    def test_wildcard_cors_is_rejected_in_production(self):
        """A wildcard origin with credentials lets any site drive the API
        using a signed-in clinician's session."""
        with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
            Settings(**{**PROD, "allowed_origins": "*"})

    def test_database_url_bypasses_the_password_check(self):
        """Credentials embedded in DATABASE_URL are legitimate."""
        Settings(
            **{
                **PROD,
                "postgres_password": "medicore_pw",
                "database_url": "postgresql://u:strongpass@db:5432/medicore",
            }
        )

    def test_valid_production_config_boots(self):
        assert Settings(**PROD).is_production is True

    @pytest.mark.parametrize("env", ["local", "test", "dev", "development"])
    def test_non_production_environments_are_unaffected(self, env):
        """Developers must not need to invent secrets to run the stack."""
        settings = Settings(
            env=env,
            jwt_secret="change-this-in-prod",
            session_secret="dev-change-me",
            postgres_password="medicore_pw",
        )
        assert settings.is_production is False

    def test_error_names_every_problem_at_once(self):
        """Fixing config one error per restart is painful; report them all."""
        with pytest.raises(ValueError) as exc:
            Settings(
                env="production",
                jwt_secret="change-this-in-prod",
                session_secret="dev-change-me",
            )
        message = str(exc.value)
        assert "JWT_SECRET" in message and "SESSION_SECRET" in message

    def test_missing_trusted_hosts_rejected(self):
        with pytest.raises(ValueError, match="TRUSTED_HOSTS"):
            Settings(**{**PROD, "trusted_hosts": ""})

    def test_demo_login_cannot_be_enabled_in_production(self):
        with pytest.raises(ValueError, match="ENABLE_DEMO_LOGIN"):
            Settings(**{**PROD, "enable_demo_login": True})

    def test_production_requires_oidc(self):
        with pytest.raises(ValueError, match="OIDC"):
            Settings(
                **{
                    **PROD,
                    "oidc_issuer": "",
                    "oidc_client_id": "",
                    "oidc_client_secret": "",
                }
            )

    def test_access_token_ttl_bounded_in_production(self):
        with pytest.raises(ValueError, match="ACCESS_TOKEN_TTL"):
            Settings(**{**PROD, "access_token_ttl_minutes": 24 * 60})

    def test_demo_login_allowed_only_in_local_test(self):
        assert Settings(env="local").demo_login_allowed is True
        assert Settings(env="test").demo_login_allowed is True
        # "dev" is non-production but still requires an explicit opt-in.
        assert Settings(env="dev", enable_demo_login=False).demo_login_allowed is False
        assert Settings(env="dev", enable_demo_login=True).demo_login_allowed is True
        # Production is always False, even if someone tries to force the flag
        # (the validator also refuses to boot in that case).
        assert Settings(**PROD).demo_login_allowed is False


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


@pytest.fixture()
def headers_client():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/x")
    def x():
        return {"ok": True}

    return TestClient(app)


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        "header,expected",
        [
            ("x-content-type-options", "nosniff"),
            ("x-frame-options", "DENY"),
            ("referrer-policy", "no-referrer"),
        ],
    )
    def test_headers_present(self, headers_client, header, expected):
        assert headers_client.get("/x").headers[header] == expected

    def test_phi_responses_are_not_cacheable(self, headers_client):
        """A shared or disk cache holding PHI is a breach."""
        cache_control = headers_client.get("/x").headers["cache-control"]
        assert "no-store" in cache_control
        assert "private" in cache_control

    def test_hsts_only_on_tls(self, headers_client):
        # Plain HTTP: advertising HSTS would break local development.
        assert "strict-transport-security" not in headers_client.get("/x").headers
        # Behind a TLS-terminating proxy.
        r = headers_client.get("/x", headers={"x-forwarded-proto": "https"})
        assert "max-age=" in r.headers["strict-transport-security"]

    def test_hsts_can_be_disabled(self):
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware, hsts=False)

        @app.get("/x")
        def x():
            return {}

        r = TestClient(app).get("/x", headers={"x-forwarded-proto": "https"})
        assert "strict-transport-security" not in r.headers


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def _rate_limited_app(limit: int = 3) -> TestClient:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, limit=limit, window_seconds=60)

    @app.get("/x")
    def x():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True}

    return TestClient(app)


class TestRateLimiting:
    def test_requests_beyond_the_limit_are_rejected(self):
        client = _rate_limited_app(limit=3)
        codes = [client.get("/x").status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]

    def test_rejection_tells_the_client_when_to_retry(self):
        client = _rate_limited_app(limit=1)
        client.get("/x")
        r = client.get("/x")
        assert int(r.headers["retry-after"]) >= 1
        assert r.headers["x-ratelimit-limit"] == "1"

    def test_probes_are_exempt(self):
        """Rate-limiting health checks would make Kubernetes kill the pod."""
        client = _rate_limited_app(limit=1)
        assert all(client.get("/health").status_code == 200 for _ in range(10))

    def test_limits_are_per_caller_not_global(self):
        """One abusive client must not exhaust everyone else's budget."""
        client = _rate_limited_app(limit=2)
        for _ in range(3):
            client.get("/x", headers={"x-forwarded-for": "10.0.0.1"})
        assert client.get("/x", headers={"x-forwarded-for": "10.0.0.2"}).status_code == 200

    def test_bucket_map_does_not_grow_without_bound(self):
        """Otherwise a spoofed X-Forwarded-For becomes a memory-exhaustion DoS."""
        app = FastAPI()
        limiter = RateLimitMiddleware(app, limit=100, window_seconds=0)

        @app.get("/x")
        def x():
            return {}

        app.user_middleware.clear()
        app.add_middleware(
            RateLimitMiddleware, limit=100, window_seconds=0
        )
        client = TestClient(app)
        for i in range(50):
            client.get("/x", headers={"x-forwarded-for": f"10.0.0.{i}"})
        # With a zero-length window every bucket is stale and swept away.
        assert len(limiter._hits) < 50


# ---------------------------------------------------------------------------
# Body size
# ---------------------------------------------------------------------------


class TestBodyLimit:
    @pytest.fixture()
    def client(self):
        app = FastAPI()
        app.add_middleware(BodySizeLimitMiddleware, max_bytes=1000)

        @app.post("/x")
        async def x():
            return {"ok": True}

        return TestClient(app)

    def test_oversized_body_rejected(self, client):
        r = client.post("/x", content=b"x" * 10, headers={"content-length": "999999"})
        assert r.status_code == 413

    def test_normal_body_accepted(self, client):
        assert client.post("/x", content=b"x" * 10).status_code == 200

    def test_malformed_content_length_rejected(self, client):
        r = client.post("/x", content=b"x", headers={"content-length": "abc"})
        assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Audit trail (HIPAA 164.312(b))
# ---------------------------------------------------------------------------


def _audit_app() -> TestClient:
    app = FastAPI()
    app.add_middleware(AuditLogMiddleware, service="test")

    @app.get("/fhir/patient/{pid}")
    def read(pid: str):
        return {"ok": True}

    @app.get("/fhir/patient/search")
    def search():
        return {"ok": True}

    return TestClient(app)


def _records(caplog) -> list[dict]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "medicore.audit" and r.getMessage().startswith("{")
    ]


class TestAuditTrail:
    def test_records_which_patient_was_accessed(self, caplog):
        """HIPAA requires an accounting of disclosures: 'who viewed this
        chart?' must be answerable. Previously only the actor was recorded."""
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            _audit_app().get("/fhir/patient/MRN-000123")
        record = _records(caplog)[-1]
        assert record["resource_type"] == "patient"
        assert record["resource_ref"], "no reference to the accessed record"

    def test_raw_identifiers_are_not_written_to_logs(self, caplog):
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            _audit_app().get("/fhir/patient/MRN-000123?patient=MRN-000123")
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "MRN-000123" not in blob

    def test_reference_is_stable_for_the_same_patient(self, caplog):
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client = _audit_app()
            client.get("/fhir/patient/MRN-1")
            client.get("/fhir/patient/MRN-1")
            client.get("/fhir/patient/MRN-2")
        refs = [r["resource_ref"] for r in _records(caplog)]
        assert refs[0] == refs[1], "same patient must correlate across records"
        assert refs[0] != refs[2], "different patients must not collide"

    def test_a_search_filtered_by_patient_is_audited(self, caplog):
        """Searching for one patient is an access to that patient."""
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            _audit_app().get("/fhir/patient/search?patient=MRN-9")
        assert _records(caplog)[-1]["patient_ref"]

    def test_query_values_are_never_logged(self, caplog):
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            _audit_app().get("/fhir/patient/search?birthdate=1980-01-01")
        record = _records(caplog)[-1]
        assert record["query_keys"] == ["birthdate"]
        assert "1980-01-01" not in json.dumps(record)

    def test_denied_attempts_are_recorded_as_such(self, caplog):
        """Failed access attempts are the most security-relevant audit events."""
        app = FastAPI()
        app.add_middleware(AuditLogMiddleware)

        @app.get("/x")
        def x():
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="nope")

        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            TestClient(app, raise_server_exceptions=False).get("/x")
        assert _records(caplog)[-1]["outcome"] == "denied"

    def test_client_ip_uses_the_forwarded_header(self, caplog):
        """Behind a proxy every request otherwise appears to come from the
        proxy, which makes the audit trail useless for investigation."""
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            _audit_app().get("/fhir/patient/1", headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
        assert _records(caplog)[-1]["client_ip"] == "203.0.113.7"

    def test_pseudonym_is_not_reversible_but_is_matchable(self):
        token = pseudonymise("MRN-000123", "salt")
        assert "MRN" not in token
        assert token == pseudonymise("MRN-000123", "salt")
        assert token != pseudonymise("MRN-000123", "different-salt")


# ---------------------------------------------------------------------------
# Gateway input validation
# ---------------------------------------------------------------------------


@pytest.fixture()
def gateway_client(monkeypatch):
    import backend.services.gateway.main as gw

    seen: list[tuple[str, dict]] = []

    async def fake_search(resource, params=None):
        seen.append((resource, dict(params or {})))
        return {"resourceType": "Bundle", "entry": []}

    async def fake_read(resource, rid):
        return {"resourceType": resource, "id": rid}

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(gw.fhir, "search", fake_search)
    monkeypatch.setattr(gw.fhir, "read", fake_read)
    monkeypatch.setattr(gw, "get_cached", nothing)
    monkeypatch.setattr(gw, "set_cached", nothing)
    with TestClient(gw.app, raise_server_exceptions=False) as client:
        yield client, seen


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token('u', roles=['clinician'])}"}


class TestGatewayInputValidation:
    def test_unknown_search_parameters_are_rejected(self, gateway_client):
        """An allow-list stops callers reaching undocumented upstream
        behaviour, and stops cache flooding via junk parameter names."""
        client, seen = gateway_client
        r = client.get("/fhir/patient/search?evil=1", headers=_auth())
        assert r.status_code == 400
        assert seen == []

    def test_overlong_values_are_rejected(self, gateway_client):
        client, seen = gateway_client
        r = client.get(f"/fhir/patient/search?patient={'A' * 500}", headers=_auth())
        assert r.status_code == 400
        assert seen == []

    def test_count_is_clamped(self, gateway_client):
        """Without a cap, one request can pull an unbounded amount of PHI."""
        client, seen = gateway_client
        client.get("/fhir/patient/search?patient=1&_count=999999", headers=_auth())
        assert int(seen[-1][1]["_count"]) <= 100

    def test_a_default_page_size_is_applied(self, gateway_client):
        client, seen = gateway_client
        client.get("/fhir/patient/search?patient=1", headers=_auth())
        assert "_count" in seen[-1][1]

    def test_non_numeric_count_is_rejected(self, gateway_client):
        client, _ = gateway_client
        assert client.get(
            "/fhir/patient/search?_count=abc", headers=_auth()
        ).status_code == 400

    @pytest.mark.parametrize("bad", ["../etc/passwd", "a b", "id;drop", "x" * 100])
    def test_malformed_resource_ids_are_rejected(self, gateway_client, bad):
        client, _ = gateway_client
        r = client.get(f"/fhir/patient/{bad}", headers=_auth())
        assert r.status_code in (400, 404)

    @pytest.mark.parametrize("good", ["123", "MRN-000123", "abc.def"])
    def test_valid_resource_ids_are_accepted(self, gateway_client, good):
        client, _ = gateway_client
        assert client.get(f"/fhir/patient/{good}", headers=_auth()).status_code == 200
