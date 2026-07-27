"""Tests for CDS scoring, cache keys and the audit logger."""

import pytest
from starlette.testclient import TestClient

from backend.common.cache import _make_key
from backend.common.security import create_access_token


def _auth(*roles: str) -> dict[str, str]:
    """CDS enforces auth itself, so these calls must carry a token."""
    return {"Authorization": f"Bearer {create_access_token('t', roles=list(roles) or ['clinician'])}"}

# --- cache keys ----------------------------------------------------------


def test_cache_key_is_order_independent():
    assert _make_key("Patient", {"a": "1", "b": "2"}) == _make_key(
        "Patient", {"b": "2", "a": "1"}
    )


def test_cache_key_separates_resources():
    assert _make_key("Patient", {"id": "1"}) != _make_key("Observation", {"id": "1"})


def test_cache_key_handles_empty_params():
    assert _make_key("Patient", {}) == _make_key("Patient", None)


# --- CDS -----------------------------------------------------------------


@pytest.fixture()
def cds():
    import backend.services.cds.main as m

    return TestClient(m.app)


def test_healthy_vitals_are_low_risk(cds):
    r = cds.post("/risk", json={"hr": 72, "sbp": 120, "spo2": 98}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["class_label"] == "low"
    assert r.json()["score"] == 0.0


def test_critical_vitals_are_high_risk(cds):
    r = cds.post("/risk", json={"hr": 190, "sbp": 60, "spo2": 80}, headers=_auth())
    assert r.json()["class_label"] == "high"


def test_good_vital_cannot_mask_bad_one(cds):
    """Regression: the old formula let high SBP cancel out tachycardia."""
    tachy = cds.post("/risk", json={"hr": 180, "sbp": 190, "spo2": 99}, headers=_auth()).json()
    assert tachy["score"] > 0.0


def test_score_is_bounded(cds):
    r = cds.post("/risk", json={"hr": 300, "sbp": 1, "spo2": 1}, headers=_auth()).json()
    assert 0.0 <= r["score"] <= 1.0


@pytest.mark.parametrize(
    "payload",
    [
        {"hr": 72, "sbp": 120, "spo2": 400},  # impossible saturation
        {"hr": -5, "sbp": 120, "spo2": 98},
        {"hr": 72, "sbp": 0, "spo2": 98},
    ],
)
def test_impossible_vitals_rejected(cds, payload):
    assert cds.post("/risk", json=payload, headers=_auth()).status_code == 422


# --- audit logging -------------------------------------------------------


def test_audit_log_redacts_phi(caplog):
    import json
    import logging

    from fastapi import FastAPI

    from backend.common.middleware import AuditLogMiddleware

    app = FastAPI()
    app.add_middleware(AuditLogMiddleware)

    @app.get("/fhir/patient/{pid}")
    def h(pid: str):
        return {"ok": True}

    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="medicore.audit"):
        client.get("/fhir/patient/SECRET-MRN-123?birthdate=1980-01-01")

    out = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET-MRN-123" not in out, "patient id must not be logged"
    assert "1980-01-01" not in out, "query values must not be logged"

    line = json.loads([ln for ln in out.strip().splitlines() if ln.startswith("{")][-1])
    assert line["path"] == "/fhir/patient/{id}"
    assert line["query_keys"] == ["birthdate"]
    assert line["status"] == 200
