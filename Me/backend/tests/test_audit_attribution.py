"""Patient attribution for non-Patient reads (SECURITY.md R11).

Reading ``Observation/obs-1`` is an access to a patient's record, but only the
resource body knows whose. Before this, the audit trail recorded the
observation's own reference, so the access never appeared in that patient's
disclosure accounting — the event was logged, but unfindable.

These cover the extractor's handling of real FHIR reference shapes, and the
end-to-end path from a gateway read to the emitted audit record.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from starlette.testclient import TestClient

from backend.common.middleware import (
    audit_reference,
    patient_id_from_resource,
)
from backend.common.security import create_access_token

# ---------------------------------------------------------------------------
# The extractor
# ---------------------------------------------------------------------------


class TestPatientIdFromResource:
    def test_relative_subject_reference(self):
        assert (
            patient_id_from_resource(
                {"resourceType": "Observation", "subject": {"reference": "Patient/123"}}
            )
            == "123"
        )

    def test_absolute_subject_reference(self):
        """Servers commonly return fully-qualified URLs."""
        assert (
            patient_id_from_resource(
                {
                    "resourceType": "Observation",
                    "subject": {"reference": "https://ehr.example/fhir/Patient/abc-1"},
                }
            )
            == "abc-1"
        )

    def test_patient_field_is_honoured(self):
        """A few R4 types use `patient` rather than `subject`."""
        assert (
            patient_id_from_resource(
                {
                    "resourceType": "AllergyIntolerance",
                    "patient": {"reference": "Patient/p-9"},
                }
            )
            == "p-9"
        )

    def test_subject_wins_over_patient_when_both_present(self):
        assert (
            patient_id_from_resource(
                {
                    "resourceType": "Observation",
                    "subject": {"reference": "Patient/from-subject"},
                    "patient": {"reference": "Patient/from-patient"},
                }
            )
            == "from-subject"
        )

    def test_a_patient_resource_is_its_own_subject(self):
        assert (
            patient_id_from_resource({"resourceType": "Patient", "id": "777"}) == "777"
        )

    def test_non_patient_subject_is_ignored(self):
        """A subject may legitimately be a Group/Device/Location. Recording
        those as a patient would put unrelated accesses in someone's
        disclosure accounting."""
        for reference in ("Group/g1", "Device/d1", "Location/l1"):
            assert (
                patient_id_from_resource(
                    {"resourceType": "Observation", "subject": {"reference": reference}}
                )
                is None
            )

    def test_contained_and_urn_references_are_not_resolved(self):
        """These identify a resource inside a bundle, not a patient id the
        rest of the system would recognise."""
        for reference in ("#p1", "urn:uuid:9f2c-not-an-id"):
            assert (
                patient_id_from_resource(
                    {"resourceType": "Observation", "subject": {"reference": reference}}
                )
                is None
            )

    @pytest.mark.parametrize(
        "resource",
        [
            None,
            "not-a-dict",
            42,
            {},
            {"resourceType": "Observation"},
            {"resourceType": "Observation", "subject": {}},
            {"resourceType": "Observation", "subject": {"reference": ""}},
            {"resourceType": "Observation", "subject": {"reference": None}},
            {"resourceType": "Observation", "subject": "Patient/123"},
            {"resourceType": "Patient"},
        ],
    )
    def test_malformed_input_returns_none_and_never_raises(self, resource):
        """An audit helper must not be able to break a clinical read."""
        assert patient_id_from_resource(resource) is None

    def test_reference_with_a_hostile_id_is_rejected(self):
        """The id ends up in an audit column; only spec-legal ids are taken."""
        assert (
            patient_id_from_resource(
                {
                    "resourceType": "Observation",
                    "subject": {"reference": "Patient/../../etc/passwd"},
                }
            )
            is None
        )

    def test_history_suffix_is_stripped(self):
        assert (
            patient_id_from_resource(
                {
                    "resourceType": "Observation",
                    "subject": {"reference": "Patient/123/_history/2"},
                }
            )
            == "123"
        )


# ---------------------------------------------------------------------------
# End to end through the gateway
# ---------------------------------------------------------------------------


OBSERVATION = {
    "resourceType": "Observation",
    "id": "obs-1",
    "status": "final",
    "subject": {"reference": "Patient/MRN-77"},
    "code": {"text": "Heart rate"},
}


@pytest.fixture()
def gateway(monkeypatch):
    import backend.services.gateway.main as gw

    resources: dict[str, Any] = {
        ("Observation", "obs-1"): OBSERVATION,
        ("Patient", "MRN-77"): {"resourceType": "Patient", "id": "MRN-77"},
        # A resource whose subject is not a patient.
        ("Observation", "obs-group"): {
            "resourceType": "Observation",
            "id": "obs-group",
            "subject": {"reference": "Group/ward-a"},
        },
        # A resource with no subject at all.
        ("Observation", "obs-bare"): {"resourceType": "Observation", "id": "obs-bare"},
    }

    async def fake_read(resource, resource_id):
        return resources[(resource, resource_id)]

    async def fake_search(resource, params=None):
        return {"resourceType": "Bundle", "entry": []}

    monkeypatch.setattr(gw.fhir, "read", fake_read)
    monkeypatch.setattr(gw.fhir, "search", fake_search)

    async def no_cache(*a, **k):
        return None

    monkeypatch.setattr(gw, "get_cached", no_cache)
    monkeypatch.setattr(gw, "set_cached", no_cache)

    with TestClient(gw.app, raise_server_exceptions=False) as client:
        yield client


def auth(roles=("clinician",)):
    return {"Authorization": f"Bearer {create_access_token('dr.reader', roles=list(roles))}"}


def records(caplog) -> list[dict]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "medicore.audit" and r.getMessage().startswith("{")
    ]


class TestGatewayAttribution:
    def test_observation_read_is_attributed_to_its_patient(self, gateway, caplog):
        """The R11 fix: this access must be findable in MRN-77's trail."""
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            assert gateway.get("/fhir/observation/obs-1", headers=auth()).status_code == 200
        record = records(caplog)[-1]
        assert record["patient_ref"] == audit_reference("MRN-77")

    def test_the_resource_reference_is_still_recorded(self, gateway, caplog):
        """Attribution adds the patient; it must not lose which record was read."""
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/observation/obs-1", headers=auth())
        record = records(caplog)[-1]
        assert record["resource_ref"] == audit_reference("obs-1")
        assert record["patient_ref"] == audit_reference("MRN-77")

    def test_raw_identifiers_are_still_not_logged(self, gateway, caplog):
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/observation/obs-1", headers=auth())
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "MRN-77" not in blob

    def test_patient_read_is_attributed_to_itself(self, gateway, caplog):
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/patient/MRN-77", headers=auth())
        record = records(caplog)[-1]
        assert record["patient_ref"] == audit_reference("MRN-77")

    def test_non_patient_subject_is_not_attributed(self, gateway, caplog):
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/observation/obs-group", headers=auth())
        assert records(caplog)[-1].get("patient_ref") is None

    def test_resource_without_a_subject_is_not_attributed(self, gateway, caplog):
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/observation/obs-bare", headers=auth())
        assert records(caplog)[-1].get("patient_ref") is None

    def test_an_explicit_query_filter_is_not_overwritten(self, gateway, caplog):
        """A search filtered by patient already identified the subject; the
        resolved value must not clobber it."""
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/observation/search?patient=MRN-OTHER", headers=auth())
        assert records(caplog)[-1]["patient_ref"] == audit_reference("MRN-OTHER")

    def test_a_denied_read_is_not_attributed(self, gateway, caplog):
        """The handler never ran, so there is no resource to attribute — but
        the denial itself must still be recorded."""
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            r = gateway.get("/fhir/observation/obs-1", headers=auth(["viewer"]))
        assert r.status_code == 403
        record = records(caplog)[-1]
        assert record["outcome"] == "denied"
        assert record.get("patient_ref") is None

    def test_attribution_reaches_the_audit_sink(self, gateway, monkeypatch, caplog):
        """The queryable index is what makes this searchable, so the resolved
        patient has to survive into the sink, not just the log line."""
        from backend.common import middleware

        seen: list[dict] = []
        monkeypatch.setattr(middleware, "_sink", seen.append)
        gateway.get("/fhir/observation/obs-1", headers=auth())
        refs = [r.get("patient_ref") for r in seen if r.get("patient_ref")]
        assert audit_reference("MRN-77") in refs

    def test_a_search_names_every_patient_it_disclosed(self, gateway, monkeypatch, caplog):
        """SECURITY.md R12: a search returning several patients discloses all
        of them, so each belongs in the record — not just the query shape."""
        import backend.services.gateway.main as gw

        async def multi_result(resource, params=None):
            return {
                "resourceType": "Bundle",
                "entry": [
                    {"resource": {"resourceType": "Patient", "id": "MRN-A"}},
                    {"resource": {"resourceType": "Patient", "id": "MRN-B"}},
                ],
            }

        monkeypatch.setattr(gw.fhir, "search", multi_result)
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/patient/search?family=Smith", headers=auth())
        record = records(caplog)[-1]
        assert record["subject_count"] == 2
        assert set(record["subject_refs"]) == {
            audit_reference("MRN-A"),
            audit_reference("MRN-B"),
        }

    def test_search_results_are_not_logged_raw(self, gateway, monkeypatch, caplog):
        import backend.services.gateway.main as gw

        async def multi_result(resource, params=None):
            return {
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Patient", "id": "MRN-LEAK"}}],
            }

        monkeypatch.setattr(gw.fhir, "search", multi_result)
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/patient/search?family=Smith", headers=auth())
        assert "MRN-LEAK" not in "\n".join(r.getMessage() for r in caplog.records)

    def test_observation_search_attributes_to_the_subjects(self, gateway, monkeypatch, caplog):
        """Non-Patient results are attributed via their subject reference."""
        import backend.services.gateway.main as gw

        async def obs_results(resource, params=None):
            return {
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "Observation",
                            "id": "o1",
                            "subject": {"reference": "Patient/MRN-OBS"},
                        }
                    }
                ],
            }

        monkeypatch.setattr(gw.fhir, "search", obs_results)
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/observation/search?date=2026-01-01", headers=auth())
        record = records(caplog)[-1]
        assert record["subject_refs"] == [audit_reference("MRN-OBS")]

    def test_the_subject_list_is_bounded(self, gateway, monkeypatch, caplog):
        """A very wide search must not write an unbounded audit row; the count
        still reports the true scale of the disclosure."""
        import backend.services.gateway.main as gw
        from backend.common.middleware import MAX_AUDITED_SUBJECTS

        async def many(resource, params=None):
            return {
                "resourceType": "Bundle",
                "entry": [
                    {"resource": {"resourceType": "Patient", "id": f"MRN-{i}"}}
                    for i in range(60)
                ],
            }

        monkeypatch.setattr(gw.fhir, "search", many)
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/patient/search?family=Common", headers=auth())
        record = records(caplog)[-1]
        assert record["subject_count"] == 60
        assert len(record["subject_refs"]) == MAX_AUDITED_SUBJECTS

    def test_an_empty_search_records_no_subjects(self, gateway, caplog):
        """Finding nobody is not a disclosure."""
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/patient/search?family=Nobody", headers=auth())
        record = records(caplog)[-1]
        assert "subject_refs" not in record
        assert "subject_count" not in record

    def test_a_cache_hit_is_still_attributed(self, gateway, monkeypatch, caplog):
        """Serving a search from cache still discloses those patients."""
        import backend.services.gateway.main as gw

        async def cached(resource, params, max_age_seconds=300):
            return {
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Patient", "id": "MRN-CACHED"}}],
            }

        monkeypatch.setattr(gw, "get_cached", cached)
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/patient/search?family=Smith", headers=auth())
        record = records(caplog)[-1]
        assert record["subject_refs"] == [audit_reference("MRN-CACHED")]

    def test_a_single_result_still_populates_patient_ref(self, gateway, monkeypatch, caplog):
        """The common 'who viewed MRN-X?' query reads patient_ref, so a
        single-result search must keep filling it."""
        import backend.services.gateway.main as gw

        async def one(resource, params=None):
            return {
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Patient", "id": "MRN-ONE"}}],
            }

        monkeypatch.setattr(gw.fhir, "search", one)
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/patient/search?family=Only", headers=auth())
        assert records(caplog)[-1]["patient_ref"] == audit_reference("MRN-ONE")

    def test_an_explicit_filter_still_wins_for_patient_ref(self, gateway, monkeypatch, caplog):
        import backend.services.gateway.main as gw

        async def one(resource, params=None):
            return {
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Patient", "id": "MRN-RESULT"}}],
            }

        monkeypatch.setattr(gw.fhir, "search", one)
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            gateway.get("/fhir/observation/search?patient=MRN-FILTER", headers=auth())
        assert records(caplog)[-1]["patient_ref"] == audit_reference("MRN-FILTER")

    def test_a_broken_resource_body_does_not_fail_the_read(self, gateway, monkeypatch):
        """Attribution is best-effort; a weird upstream payload must not turn
        a successful clinical read into an error."""
        import backend.services.gateway.main as gw

        async def weird_read(resource, resource_id):
            return {"resourceType": "Observation", "subject": ["not", "a", "dict"]}

        monkeypatch.setattr(gw.fhir, "read", weird_read)
        assert gateway.get("/fhir/observation/obs-1", headers=auth()).status_code == 200
