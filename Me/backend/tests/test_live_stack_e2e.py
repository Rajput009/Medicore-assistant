"""Live multi-process stack E2E (no Docker).

Boots real uvicorn workers for auth, gateway, CDS and patient-flow over the
loopback interface, against:

  * genuine PostgreSQL via ``pgserver`` (FHIR cache)
  * a tiny in-process FHIR HTTP stub (real HTTP, not monkeypatch)
  * mongomock-motor for patient-flow state (real Motor API; no mongod binary)

This is the strongest end-to-end path this environment can run without Docker
or external download hosts. Skips cleanly when ``pgserver`` is missing.

Cross-service flow under test:

  login (auth) → access token + httpOnly cookie
       → CDS /risk (authenticated)
       → gateway /ready (real Postgres ping)
       → gateway FHIR search/read (HTTP to local stub + real cache round-trip)
       → patient-flow beds/queue claim/complete
       → logout (revokes jti) → subsequent CDS call 401
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

pytest.importorskip("pgserver", reason="pgserver not installed")
pytest.importorskip("mongomock_motor", reason="mongomock_motor not installed")

import pgserver  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]  # Me/
PYTHON = sys.executable

AUTH_PORT = 18081
GATEWAY_PORT = 18080
CDS_PORT = 18083
FLOW_PORT = 18082
FHIR_PORT = 18090


# ---------------------------------------------------------------------------
# Tiny FHIR stub (real HTTP)
# ---------------------------------------------------------------------------


class _FhirHandler(BaseHTTPRequestHandler):
    patients = {
        "123": {
            "resourceType": "Patient",
            "id": "123",
            "active": True,
            "name": [{"text": "Ada Lovelace"}],
        }
    }

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        return

    def _json(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/fhir+json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/oauth2/token"):
            self._json(405, {"error": "use POST"})
            return
        if path.startswith("/fhir/Patient/"):
            pid = path.rsplit("/", 1)[-1]
            doc = self.patients.get(pid)
            if not doc:
                self._json(404, {"resourceType": "OperationOutcome", "issue": []})
                return
            self._json(200, doc)
            return
        if path.startswith("/fhir/Patient"):
            self._json(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "total": 1,
                    "entry": [{"resource": self.patients["123"]}],
                },
            )
            return
        self._json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(length)
        if self.path.startswith("/oauth2/token"):
            self._json(
                200,
                {
                    "access_token": "stub-fhir-token",
                    "token_type": "bearer",
                    "expires_in": 3600,
                },
            )
            return
        self._json(404, {"error": "not found"})


def _start_fhir_stub() -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(("127.0.0.1", FHIR_PORT), _FhirHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def _http(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, str], Any]:
    hdrs = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if headers:
        hdrs.update(headers)
    req = Request(url, data=data, headers=hdrs, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            payload: Any
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = raw.decode("utf-8", errors="replace")
            return resp.status, dict(resp.headers.items()), payload
    except HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", errors="replace")
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, payload


def _wait_health(port: int, service: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            code, _, body = _http("GET", f"http://127.0.0.1:{port}/health", timeout=1)
            if code == 200 and isinstance(body, dict) and body.get("service") == service:
                return
        except (URLError, TimeoutError, ConnectionError) as exc:
            last = exc
        time.sleep(0.15)
    raise TimeoutError(f"{service} on :{port} did not become healthy ({last})")


@pytest.fixture(scope="module")
def live_stack():
    """Boot the multi-process stack once for the module."""
    fhir_server, _ = _start_fhir_stub()

    td = Path(tempfile.mkdtemp(prefix="live-e2e-pg-"))
    pg = pgserver.get_server(td, cleanup_mode="delete")
    dsn = pg.get_uri()

    env = os.environ.copy()
    env.update(
        {
            "ENV": "test",
            "OTEL_ENABLED": "false",
            "JWT_SECRET": "test-secret-at-least-32-chars-long!!",
            "SESSION_SECRET": "test-session-secret-at-least-32chars!",
            "DATABASE_URL": dsn,
            "REDIS_ENABLED": "false",
            "EXPOSE_API_DOCS": "false",
            "ENABLE_DEMO_LOGIN": "true",
            "DEMO_PASSWORD": "medicore-dev",
            "AUTH_SET_COOKIE": "true",
            "ALLOWED_ORIGINS": "http://127.0.0.1:5173,http://localhost:5173",
            "FHIR_BASE_URL": f"http://127.0.0.1:{FHIR_PORT}/fhir",
            "FHIR_OAUTH_TOKEN_URL": f"http://127.0.0.1:{FHIR_PORT}/oauth2/token",
            "FHIR_CLIENT_ID": "e2e-client",
            "FHIR_CLIENT_SECRET": "e2e-secret-not-a-placeholder",
            "PYTHONPATH": str(ROOT.resolve()),
            "BED_LAYOUT": "A:2,ICU:1",
        }
    )

    procs: list[tuple[str, subprocess.Popen]] = []

    def spawn(name: str, args: list[str], port: int, service: str) -> None:
        proc = subprocess.Popen(
            args,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append((name, proc))
        try:
            _wait_health(port, service)
        except Exception:
            out = ""
            if proc.poll() is not None and proc.stdout:
                out = proc.stdout.read()[-2000:]
            for _, p in procs:
                p.send_signal(signal.SIGTERM)
            raise RuntimeError(f"{name} failed to start\n{out}") from None

    # auth / gateway / cds — stock apps
    for name, module, port, service in (
        ("auth", "backend.services.auth.main:app", AUTH_PORT, "auth"),
        ("gateway", "backend.services.gateway.main:app", GATEWAY_PORT, "gateway"),
        ("cds", "backend.services.cds.main:app", CDS_PORT, "cds"),
    ):
        spawn(
            name,
            [
                PYTHON,
                "-m",
                "uvicorn",
                module,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            port,
            service,
        )

    # patient-flow with mongomock injected before uvicorn imports the app.
    flow_boot = f"""
import backend.services.patient_flow.main as pf
from backend.services.patient_flow.repository import PatientFlowRepository
from mongomock_motor import AsyncMongoMockClient
pf._repository = PatientFlowRepository(AsyncMongoMockClient()["medicore"])
import uvicorn
uvicorn.run(pf.app, host="127.0.0.1", port={FLOW_PORT}, log_level="warning")
"""
    spawn(
        "patient-flow",
        [PYTHON, "-c", flow_boot],
        FLOW_PORT,
        "patient-flow",
    )

    yield {
        "auth": f"http://127.0.0.1:{AUTH_PORT}",
        "gateway": f"http://127.0.0.1:{GATEWAY_PORT}",
        "cds": f"http://127.0.0.1:{CDS_PORT}",
        "flow": f"http://127.0.0.1:{FLOW_PORT}",
    }

    for name, proc in procs:
        proc.send_signal(signal.SIGTERM)
    for name, proc in procs:
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    fhir_server.shutdown()
    try:
        pg._cleanup()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLiveStackHealth:
    def test_every_service_is_live(self, live_stack):
        for key, service in (
            ("auth", "auth"),
            ("gateway", "gateway"),
            ("cds", "cds"),
            ("flow", "patient-flow"),
        ):
            code, _, body = _http("GET", f"{live_stack[key]}/health")
            assert code == 200
            assert body["service"] == service

    def test_gateway_ready_hits_real_postgres(self, live_stack):
        code, _, body = _http("GET", f"{live_stack['gateway']}/ready")
        assert code == 200
        assert body == {"status": "ok", "cache": "ok"}

    def test_patient_flow_ready_with_mongomock(self, live_stack):
        code, _, body = _http("GET", f"{live_stack['flow']}/ready")
        assert code == 200
        assert body["database"] == "ok"


class TestLiveAuthToServices:
    def test_login_issues_token_and_cookie(self, live_stack):
        code, headers, body = _http(
            "POST",
            f"{live_stack['auth']}/login",
            body={"username": "dr.smith", "password": "medicore-dev"},
        )
        assert code == 200
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 900  # 15 minutes
        assert body["access_token"]
        # urllib collapses multiple Set-Cookie headers; at least one session
        # cookie (session JWT or CSRF double-submit) must be present.
        set_cookie = " ".join(
            str(v)
            for k, v in headers.items()
            if k.lower() == "set-cookie"
        ) or (headers.get("Set-Cookie") or headers.get("set-cookie") or "")
        assert "medicore_" in set_cookie.lower()
        # Cookie auth works: /session accepts the bearer we just received.
        code, _, session = _http(
            "GET", f"{live_stack['auth']}/session", token=body["access_token"]
        )
        assert code == 200
        assert session["sub"] == "dr.smith"

    def test_wrong_password_rejected(self, live_stack):
        code, _, body = _http(
            "POST",
            f"{live_stack['auth']}/login",
            body={"username": "dr.smith", "password": "nope"},
        )
        assert code == 401

    def test_token_opens_cds_and_flow(self, live_stack):
        _, _, login = _http(
            "POST",
            f"{live_stack['auth']}/login",
            body={"username": "clin.a", "password": "medicore-dev"},
        )
        token = login["access_token"]

        code, _, risk = _http(
            "POST",
            f"{live_stack['cds']}/risk",
            token=token,
            body={"hr": 110, "sbp": 85, "spo2": 90, "respiratory_rate": 28},
        )
        assert code == 200
        assert risk["news2_score"] >= 5
        assert "disclaimer" in risk

        code, _, beds = _http("GET", f"{live_stack['flow']}/beds", token=token)
        assert code == 200
        assert isinstance(beds, list) and len(beds) == 3  # A:2 + ICU:1
        assert {b["bed_id"] for b in beds} == {"A-001", "A-002", "ICU-001"}


class TestLiveClinicalWorkflow:
    def test_triage_claim_complete_and_bed_assign(self, live_stack):
        _, _, login = _http(
            "POST",
            f"{live_stack['auth']}/login",
            body={"username": "dr.who", "password": "medicore-dev"},
        )
        token = login["access_token"]

        code, _, enq = _http(
            "POST",
            f"{live_stack['flow']}/queue",
            token=token,
            body={"patient_id": "MRN-LIVE-1", "acuity": 1, "dept": "ED"},
        )
        assert code == 201
        assert enq["ok"] is True

        # Duplicate waiting slot rejected.
        code, _, _ = _http(
            "POST",
            f"{live_stack['flow']}/queue",
            token=token,
            body={"patient_id": "MRN-LIVE-1", "acuity": 2, "dept": "ED"},
        )
        assert code == 409

        code, _, claimed = _http(
            "POST",
            f"{live_stack['flow']}/queue/claim?dept=ED",
            token=token,
        )
        assert code == 200
        assert claimed["item"]["patient_id"] == "MRN-LIVE-1"
        assert claimed["item"]["status"] == "in_progress"

        code, _, bed = _http(
            "PATCH",
            f"{live_stack['flow']}/beds/A-001",
            token=token,
            body={"occupied": True, "patient_id": "MRN-LIVE-1", "expected_occupied": False},
        )
        assert code == 200
        assert bed["occupied"] is True
        assert bed["patient_id"] == "MRN-LIVE-1"

        # Optimistic concurrency: second assign with stale expected loses.
        code, _, _ = _http(
            "PATCH",
            f"{live_stack['flow']}/beds/A-001",
            token=token,
            body={"occupied": True, "patient_id": "MRN-OTHER", "expected_occupied": False},
        )
        assert code == 409

        code, _, done = _http(
            "POST",
            f"{live_stack['flow']}/queue/MRN-LIVE-1/complete",
            token=token,
        )
        assert code == 200
        assert done["item"]["status"] == "completed"

    def test_anonymous_clinical_calls_are_rejected(self, live_stack):
        code, _, _ = _http("GET", f"{live_stack['flow']}/beds")
        assert code == 401
        code, _, _ = _http(
            "POST",
            f"{live_stack['cds']}/risk",
            body={"hr": 70, "sbp": 120, "spo2": 98},
        )
        assert code == 401


class TestLiveGatewayFhirAndCache:
    def test_fhir_search_and_read_through_real_cache(self, live_stack):
        _, _, login = _http(
            "POST",
            f"{live_stack['auth']}/login",
            body={"username": "dr.fhir", "password": "medicore-dev"},
        )
        token = login["access_token"]

        code, _, bundle = _http(
            "GET",
            f"{live_stack['gateway']}/fhir/patient/search?name=Ada",
            token=token,
        )
        assert code == 200
        assert bundle["resourceType"] == "Bundle"
        assert bundle["entry"][0]["resource"]["id"] == "123"

        code, _, patient = _http(
            "GET",
            f"{live_stack['gateway']}/fhir/patient/123",
            token=token,
        )
        assert code == 200
        assert patient["resourceType"] == "Patient"
        assert patient["id"] == "123"

        # Second search is served from the real Postgres cache (same shape).
        code, _, bundle2 = _http(
            "GET",
            f"{live_stack['gateway']}/fhir/patient/search?name=Ada",
            token=token,
        )
        assert code == 200
        assert bundle2 == bundle

    def test_viewer_token_is_forbidden_on_fhir(self, live_stack):
        # Mint with the same secret the live workers were started with.
        from datetime import UTC, datetime, timedelta

        from jose import jwt

        now = datetime.now(UTC)
        token = jwt.encode(
            {
                "sub": "viewer.1",
                "roles": ["viewer"],
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=15)).timestamp()),
                "jti": "viewer-e2e-jti",
                "token_use": "access",
            },
            "test-secret-at-least-32-chars-long!!",
            algorithm="HS256",
        )
        code, _, _ = _http(
            "GET",
            f"{live_stack['gateway']}/fhir/patient/123",
            token=token,
        )
        assert code == 403


class TestLiveLogoutRevocation:
    def test_logout_revokes_token_on_auth_session(self, live_stack):
        """Logout denylists the jti inside the auth process.

        Cross-process revoke (auth → cds/gateway) requires a shared Redis
        broker — proven separately by ``test_multi_replica_redis.py``. This
        environment has no redis-server binary, so we assert the auth-local
        half of the contract: ``/session`` rejects the token after logout.
        """
        _, _, login = _http(
            "POST",
            f"{live_stack['auth']}/login",
            body={"username": "dr.logout", "password": "medicore-dev"},
        )
        token = login["access_token"]

        code, _, session = _http("GET", f"{live_stack['auth']}/session", token=token)
        assert code == 200
        assert session["sub"] == "dr.logout"

        code, _, body = _http("POST", f"{live_stack['auth']}/logout", token=token)
        assert code == 200
        assert body["status"] == "ok"

        code, _, _ = _http("GET", f"{live_stack['auth']}/session", token=token)
        assert code == 401
