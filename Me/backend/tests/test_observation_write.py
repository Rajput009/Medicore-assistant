"""Vitals capture: FHIR Observation writes.

NEWS2 was a calculator whose inputs vanished on submit. These cover the write
path that makes the readings longitudinal: correct LOINC/UCUM coding, the
encounter link, idempotent retries, cache invalidation, and the guard rails
that keep a write endpoint from becoming an arbitrary-FHIR passthrough.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from backend.common.idempotency import reset_idempotency_store
from backend.common.security import create_access_token
from backend.services.gateway.observations import (
    LOINC,
    UCUM,
    VITALS_BY_KEY,
    build_consciousness_observation,
    build_news2_observation,
    build_vital_observation,
    build_vitals_bundle,
)


@pytest.fixture(autouse=True)
def _clean_idempotency():
    reset_idempotency_store()
    yield
    reset_idempotency_store()


@pytest.fixture()
def gateway(monkeypatch):
    import backend.services.gateway.main as gw

    calls: dict[str, Any] = {"created": [], "invalidated": []}

    async def fake_create(resource, body):
        calls["created"].append((resource, body))
        return {**dict(body), "id": f"obs-{len(calls['created'])}"}

    async def fake_invalidate(resource, patient_id=None):
        calls["invalidated"].append((resource, patient_id))
        return 1

    async def no_cache(*a, **k):
        return None

    monkeypatch.setattr(gw.fhir, "create", fake_create)
    monkeypatch.setattr(gw, "invalidate_cache", fake_invalidate)
    monkeypatch.setattr(gw, "get_cached", no_cache)
    monkeypatch.setattr(gw, "set_cached", no_cache)

    with TestClient(gw.app, raise_server_exceptions=False) as client:
        yield client, calls


def auth(*roles: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token('dr.smith', roles=list(roles))}"}


FULL_VITALS = {
    "patient_id": "MRN-1",
    "respiratory_rate": 18,
    "spo2": 97,
    "temperature": 37.1,
    "systolic_bp": 120,
    "pulse": 72,
    "consciousness": "A",
    "news2_score": 0,
}


class TestResourceShape:
    """A reading is only useful to other systems if it is coded correctly."""

    def test_vital_uses_loinc_and_ucum(self):
        obs = build_vital_observation(VITALS_BY_KEY["pulse"], 72, "MRN-1")
        coding = obs["code"]["coding"][0]
        assert coding["system"] == LOINC
        assert coding["code"] == "8867-4"  # Heart rate
        assert obs["valueQuantity"]["system"] == UCUM
        assert obs["valueQuantity"]["code"] == "/min"
        assert obs["valueQuantity"]["value"] == 72

    def test_vital_is_categorised_as_a_vital_sign(self):
        obs = build_vital_observation(VITALS_BY_KEY["spo2"], 97, "MRN-1")
        assert obs["category"][0]["coding"][0]["code"] == "vital-signs"

    def test_status_is_final_not_preliminary(self):
        obs = build_vital_observation(VITALS_BY_KEY["spo2"], 97, "MRN-1")
        assert obs["status"] == "final"

    def test_subject_references_the_patient(self):
        obs = build_vital_observation(VITALS_BY_KEY["spo2"], 97, "MRN-1")
        assert obs["subject"]["reference"] == "Patient/MRN-1"

    def test_encounter_is_linked_when_supplied(self):
        obs = build_vital_observation(
            VITALS_BY_KEY["spo2"], 97, "MRN-1", encounter_id="ENC-9"
        )
        assert obs["encounter"]["reference"] == "Encounter/ENC-9"

    def test_encounter_is_absent_when_not_supplied(self):
        obs = build_vital_observation(VITALS_BY_KEY["spo2"], 97, "MRN-1")
        assert "encounter" not in obs

    def test_performer_records_who_took_the_reading(self):
        obs = build_vital_observation(
            VITALS_BY_KEY["spo2"], 97, "MRN-1", performer="dr.smith"
        )
        assert obs["performer"][0]["display"] == "dr.smith"

    def test_consciousness_is_a_string_not_a_quantity(self):
        obs = build_consciousness_observation("V", "MRN-1")
        assert obs["valueString"] == "Voice"
        assert "valueQuantity" not in obs

    def test_news2_total_is_a_survey_not_a_vital_sign(self):
        """Filing a derived score as a vital sign corrupts flowsheets."""
        obs = build_news2_observation(7, "MRN-1")
        assert obs["category"][0]["coding"][0]["code"] == "survey"
        assert obs["valueInteger"] == 7


class TestBundle:
    def test_every_reading_shares_one_timestamp(self):
        """So a flowsheet groups them into a single column."""
        resources = build_vitals_bundle(
            {"pulse": 72, "spo2": 97, "temperature": 37.0},
            "MRN-1",
            consciousness="A",
            news2_score=0,
        )
        times = {r["effectiveDateTime"] for r in resources}
        assert len(times) == 1

    def test_bundle_covers_vitals_consciousness_and_score(self):
        resources = build_vitals_bundle(
            {"pulse": 72, "spo2": 97}, "MRN-1", consciousness="A", news2_score=3
        )
        assert len(resources) == 4

    def test_unknown_vital_keys_are_ignored(self):
        resources = build_vitals_bundle({"pulse": 72, "shoe_size": 9}, "MRN-1")
        assert len(resources) == 1

    def test_omitting_the_score_omits_the_survey_observation(self):
        resources = build_vitals_bundle({"pulse": 72}, "MRN-1")
        assert all("valueInteger" not in r for r in resources)


class TestWriteEndpoint:
    def test_creates_one_observation_per_reading(self, gateway):
        client, calls = gateway
        r = client.post("/fhir/observation", json=FULL_VITALS, headers=auth("clinician"))
        assert r.status_code == 201, r.text
        # 5 vitals + consciousness + NEWS2 total.
        assert r.json()["count"] == 7
        assert len(calls["created"]) == 7

    def test_partial_vitals_are_accepted(self, gateway):
        client, calls = gateway
        r = client.post(
            "/fhir/observation",
            json={"patient_id": "MRN-1", "pulse": 80},
            headers=auth("clinician"),
        )
        assert r.status_code == 201
        assert len(calls["created"]) == 1

    def test_invalidates_the_observation_cache_for_that_patient(self, gateway):
        """Otherwise the clinician saves a reading and cannot see it."""
        client, calls = gateway
        client.post("/fhir/observation", json=FULL_VITALS, headers=auth("clinician"))
        assert ("Observation", "MRN-1") in calls["invalidated"]

    def test_retry_with_the_same_key_does_not_double_file(self, gateway):
        client, calls = gateway
        headers = {**auth("clinician"), "Idempotency-Key": "vitals-1"}

        first = client.post("/fhir/observation", json=FULL_VITALS, headers=headers)
        assert first.status_code == 201
        written = len(calls["created"])

        second = client.post("/fhir/observation", json=FULL_VITALS, headers=headers)
        assert second.status_code == 201
        assert second.json() == first.json()
        assert len(calls["created"]) == written

    def test_a_new_key_files_a_new_set_of_readings(self, gateway):
        client, calls = gateway
        client.post(
            "/fhir/observation",
            json=FULL_VITALS,
            headers={**auth("clinician"), "Idempotency-Key": "a"},
        )
        client.post(
            "/fhir/observation",
            json=FULL_VITALS,
            headers={**auth("clinician"), "Idempotency-Key": "b"},
        )
        assert len(calls["created"]) == 14


class TestWriteGuardRails:
    def test_requires_authentication(self, gateway):
        client, _ = gateway
        assert client.post("/fhir/observation", json=FULL_VITALS).status_code == 401

    def test_viewer_role_cannot_write(self, gateway):
        client, calls = gateway
        r = client.post("/fhir/observation", json=FULL_VITALS, headers=auth("viewer"))
        assert r.status_code == 403
        assert calls["created"] == []

    def test_empty_submission_is_rejected(self, gateway):
        client, calls = gateway
        r = client.post(
            "/fhir/observation", json={"patient_id": "MRN-1"}, headers=auth("clinician")
        )
        assert r.status_code == 422
        assert calls["created"] == []

    def test_physiologically_impossible_values_are_rejected(self, gateway):
        client, calls = gateway
        r = client.post(
            "/fhir/observation",
            json={"patient_id": "MRN-1", "pulse": 5000},
            headers=auth("clinician"),
        )
        assert r.status_code == 422
        assert calls["created"] == []

    def test_malformed_patient_id_is_rejected(self, gateway):
        client, calls = gateway
        r = client.post(
            "/fhir/observation",
            json={"patient_id": "../../etc/passwd", "pulse": 72},
            headers=auth("clinician"),
        )
        assert r.status_code in (400, 422)
        assert calls["created"] == []

    def test_invalid_acvpu_letter_is_rejected(self, gateway):
        client, _ = gateway
        r = client.post(
            "/fhir/observation",
            json={"patient_id": "MRN-1", "pulse": 72, "consciousness": "Z"},
            headers=auth("clinician"),
        )
        assert r.status_code == 422

    def test_upstream_failure_reports_how_many_were_saved(self, gateway, monkeypatch):
        """Partial success must be visible: the clinician has to know whether
        to re-enter the remaining readings."""
        import backend.services.gateway.main as gw
        from backend.common.fhir_client import FHIRError

        calls = {"n": 0}

        async def flaky_create(resource, body):
            calls["n"] += 1
            if calls["n"] > 2:
                raise FHIRError("upstream exploded", status_code=500)
            return {**dict(body), "id": f"obs-{calls['n']}"}

        monkeypatch.setattr(gw.fhir, "create", flaky_create)
        client, _ = gateway
        r = client.post("/fhir/observation", json=FULL_VITALS, headers=auth("clinician"))
        assert r.status_code == 502
        assert "2 of 7" in r.json()["detail"]

    def test_upstream_error_details_are_not_echoed(self, gateway, monkeypatch):
        import backend.services.gateway.main as gw
        from backend.common.fhir_client import FHIRError

        async def boom(resource, body):
            raise FHIRError("postgres://user:hunter2@db/fhir exploded", status_code=500)

        monkeypatch.setattr(gw.fhir, "create", boom)
        client, _ = gateway
        r = client.post("/fhir/observation", json=FULL_VITALS, headers=auth("clinician"))
        assert "hunter2" not in r.text
