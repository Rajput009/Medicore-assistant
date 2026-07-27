"""Grounded chart Q&A: retrieval, citation, and above all refusal.

An assistant in a hospital is judged by what it declines to say. Most of these
tests assert a *negative*: that it does not invent a value, does not report a
failed lookup as an absence, does not give advice, and does not see more than
the clinician asking.

The single most dangerous output this feature could produce is
"no allergies recorded" when the allergy search failed, so that case is
covered from several directions.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from starlette.testclient import TestClient

from backend.common.security import create_access_token
from backend.services.gateway import assist

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def obs(oid: str, label: str, value: Any, when: str, unit: str = "mmol/L") -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": oid,
        "status": "final",
        "code": {"text": label},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": when,
        "subject": {"reference": "Patient/MRN-1"},
    }


def allergy(aid: str, label: str, criticality: str = "low", reaction: str | None = None):
    resource: dict[str, Any] = {
        "resourceType": "AllergyIntolerance",
        "id": aid,
        "code": {"text": label},
        "criticality": criticality,
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "patient": {"reference": "Patient/MRN-1"},
    }
    if reaction:
        resource["reaction"] = [{"manifestation": [{"text": reaction}]}]
    return resource


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize(
        "question,expected",
        [
            ("What allergies does this patient have?", "allergies"),
            ("Any drug intolerances?", "allergies"),
            ("What medications is she taking?", "medications"),
            ("List the problems", "problems"),
            ("What was the last potassium?", "observations"),
            ("Show me the latest vitals", "observations"),
            ("Why was he admitted?", "encounters"),
        ],
    )
    def test_recognises_the_topic(self, question, expected):
        assert expected in assist.classify(question)

    def test_a_question_can_span_topics(self):
        intents = assist.classify("allergies and current medications?")
        assert "allergies" in intents and "medications" in intents

    def test_safety_critical_topics_come_first(self):
        """Ordering is stable and puts allergies ahead of observations, so a
        truncated answer keeps the most consequential part."""
        intents = assist.classify("vitals and allergies")
        assert intents.index("allergies") < intents.index("observations")

    def test_an_unrelated_question_matches_nothing(self):
        assert assist.classify("what is the wifi password") == ()

    def test_an_empty_question_matches_nothing(self):
        assert assist.classify("") == ()
        assert assist.classify("   ") == ()

    def test_the_question_is_bounded(self):
        assert len(assist.normalise_question("x" * 5000)) == assist.MAX_QUESTION_LENGTH

    def test_analyte_is_extracted_when_named(self):
        assert assist.analyte_filter("last potassium please") == "potassium"
        assert assist.analyte_filter("show me the vitals") is None


class TestAdviceRefusal:
    @pytest.mark.parametrize(
        "question",
        [
            "Should I give amoxicillin?",
            "What should I prescribe for this?",
            "Can I discharge her?",
            "Is it safe to start heparin?",
            "Recommend a treatment plan",
            "Do I stop the metformin?",
        ],
    )
    def test_decision_questions_are_recognised(self, question):
        assert assist.requests_advice(question) is True

    @pytest.mark.parametrize(
        "question",
        [
            "What allergies are recorded?",
            "What was the last potassium?",
            "List current medications",
        ],
    )
    def test_factual_questions_are_not_refused(self, question):
        assert assist.requests_advice(question) is False

    def test_the_refusal_explains_what_to_ask_instead(self):
        answer = assist.advice_refusal()
        assert answer.answered is False
        assert answer.findings == ()
        assert any("does not give clinical advice" in c for c in answer.caveats)
        assert any("Rephrase" in c for c in answer.caveats)


# ---------------------------------------------------------------------------
# The allergy rule — the one that could kill someone
# ---------------------------------------------------------------------------


class TestAllergyFailureIsNotAbsence:
    def test_a_failed_lookup_never_reads_as_no_allergies(self):
        evidence = assist.Evidence(failed={"allergies"})
        answer = assist.answer_question("any allergies?", evidence)

        assert answer.answered is False
        blob = " ".join(answer.caveats).lower()
        assert "could not be retrieved" in blob
        # The wording must actively contradict the dangerous reading.
        assert "not a statement" in blob
        assert "no allergies" not in blob.replace("has no allergies", "")

    def test_an_empty_list_is_reported_as_unconfirmed(self):
        """No record found is not the same as confirmed 'no known allergies'."""
        answer = assist.answer_question("allergies?", assist.Evidence(allergies=[]))
        blob = " ".join(answer.caveats).lower()
        assert "absence of a record is not the same" in blob

    def test_the_two_cases_produce_different_text(self):
        failed = assist.answer_question("allergies?", assist.Evidence(failed={"allergies"}))
        empty = assist.answer_question("allergies?", assist.Evidence(allergies=[]))
        assert failed.caveats != empty.caveats

    def test_a_high_criticality_allergy_is_flagged(self):
        evidence = assist.Evidence(
            allergies=[allergy("a1", "Penicillin", "high", "anaphylaxis")]
        )
        answer = assist.answer_question("allergies?", evidence)
        finding = answer.findings[0]
        assert finding.critical is True
        assert "Penicillin" in finding.text
        assert "anaphylaxis" in finding.text

    def test_critical_allergies_survive_truncation(self):
        """A long chart must not push the anaphylaxis row off the end."""
        evidence = assist.Evidence(
            allergies=[allergy(f"a{i}", f"Drug {i}") for i in range(20)]
            + [allergy("crit", "Penicillin", "high")],
            observations=[obs(f"o{i}", "Potassium", 4.0, "2026-07-01") for i in range(20)],
        )
        answer = assist.answer_question("allergies and potassium", evidence)
        assert answer.findings[0].critical is True
        assert "Penicillin" in answer.findings[0].text


# ---------------------------------------------------------------------------
# Grounding: every claim traceable, no invented values
# ---------------------------------------------------------------------------


class TestGrounding:
    def test_every_finding_carries_a_citation(self):
        evidence = assist.Evidence(
            allergies=[allergy("a1", "Penicillin")],
            observations=[obs("o1", "Potassium", 5.4, "2026-07-20")],
        )
        answer = assist.answer_question("allergies and potassium", evidence)
        assert answer.findings
        for finding in answer.findings:
            assert finding.citations
            for citation in finding.citations:
                assert citation.resource_id
                assert citation.resource_type

    def test_the_cited_id_is_the_resource_the_value_came_from(self):
        evidence = assist.Evidence(observations=[obs("obs-42", "Potassium", 5.4, "2026-07-20")])
        answer = assist.answer_question("last potassium?", evidence)
        assert answer.findings[0].citations[0].resource_id == "obs-42"

    def test_reported_values_appear_verbatim_from_the_resource(self):
        """The composer copies values; it does not compute or round them."""
        evidence = assist.Evidence(observations=[obs("o1", "Potassium", 5.4, "2026-07-20")])
        answer = assist.answer_question("last potassium?", evidence)
        assert "5.4" in answer.findings[0].text
        assert "mmol/L" in answer.findings[0].text

    def test_an_uncited_finding_is_rejected(self):
        """The guardrail that a future model-backed composer must also pass."""

        class RogueComposer:
            def compose(self, question, intents, evidence):
                return assist.Answer(
                    intents=intents,
                    findings=(assist.Finding(text="Potassium is 9.9", citations=()),),
                    caveats=(),
                    answered=True,
                )

        with pytest.raises(assist.UncitedClaimError):
            assist.answer_question("potassium?", assist.Evidence(), RogueComposer())

    def test_a_citation_without_a_resource_id_is_rejected(self):
        class SloppyComposer:
            def compose(self, question, intents, evidence):
                return assist.Answer(
                    intents=intents,
                    findings=(
                        assist.Finding(
                            text="Potassium 5.4",
                            citations=(
                                assist.Citation(
                                    resource_type="Observation", resource_id="", label="K+"
                                ),
                            ),
                        ),
                    ),
                    caveats=(),
                    answered=True,
                )

        with pytest.raises(assist.UncitedClaimError):
            assist.answer_question("potassium?", assist.Evidence(), SloppyComposer())

    def test_claiming_answered_with_no_findings_is_rejected(self):
        class EmptyBoast:
            def compose(self, question, intents, evidence):
                return assist.Answer(
                    intents=intents, findings=(), caveats=(), answered=True
                )

        with pytest.raises(assist.UncitedClaimError):
            assist.answer_question("potassium?", assist.Evidence(), EmptyBoast())


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


class TestObservations:
    def test_latest_value_wins_and_earlier_ones_are_shown_as_trend(self):
        evidence = assist.Evidence(
            observations=[
                obs("o1", "Potassium", 4.1, "2026-07-01"),
                obs("o3", "Potassium", 5.4, "2026-07-20"),
                obs("o2", "Potassium", 4.8, "2026-07-10"),
            ]
        )
        answer = assist.answer_question("last potassium?", evidence)
        text = answer.findings[0].text
        assert "Latest Potassium: 5.4" in text
        # Trend context, newest first.
        assert "4.8" in text and "4.1" in text

    def test_a_named_analyte_filters_out_everything_else(self):
        evidence = assist.Evidence(
            observations=[
                obs("o1", "Potassium", 5.4, "2026-07-20"),
                obs("o2", "Sodium", 139, "2026-07-20"),
            ]
        )
        answer = assist.answer_question("what was the last potassium?", evidence)
        assert len(answer.findings) == 1
        assert "Potassium" in answer.findings[0].text

    def test_a_missing_analyte_says_so_rather_than_answering_with_another(self):
        """Answering 'sodium' to a question about potassium would be worse
        than refusing."""
        evidence = assist.Evidence(observations=[obs("o2", "Sodium", 139, "2026-07-20")])
        answer = assist.answer_question("last potassium?", evidence)
        assert answer.answered is False
        assert any("potassium" in c.lower() for c in answer.caveats)

    def test_an_unreadable_value_is_reported_not_skipped(self):
        """A silently dropped result reads as 'not measured'."""
        weird = {
            "resourceType": "Observation",
            "id": "o9",
            "code": {"text": "Potassium"},
            "valueRatio": {"numerator": 1},
            "effectiveDateTime": "2026-07-20",
        }
        answer = assist.answer_question("potassium?", assist.Evidence(observations=[weird]))
        assert "could not be read" in answer.findings[0].text
        assert answer.findings[0].citations[0].resource_id == "o9"

    def test_undated_observations_are_kept(self):
        undated = {
            "resourceType": "Observation",
            "id": "o-undated",
            "code": {"text": "Potassium"},
            "valueQuantity": {"value": 5.0, "unit": "mmol/L"},
        }
        answer = assist.answer_question("potassium?", assist.Evidence(observations=[undated]))
        assert answer.answered is True


# ---------------------------------------------------------------------------
# Robustness — a messy chart is when this matters most
# ---------------------------------------------------------------------------


class TestMalformedResources:
    @pytest.mark.parametrize(
        "resource",
        [
            {},
            {"resourceType": "Observation"},
            {"resourceType": "Observation", "code": None},
            {"resourceType": "Observation", "code": {"coding": []}},
            {"resourceType": "Observation", "valueQuantity": "not-a-dict"},
        ],
    )
    def test_a_malformed_resource_does_not_raise(self, resource):
        answer = assist.answer_question("vitals?", assist.Evidence(observations=[resource]))
        assert isinstance(answer, assist.Answer)

    def test_codeable_text_handles_every_shape(self):
        assert assist.codeable_text({"text": "Potassium"}) == "Potassium"
        assert assist.codeable_text({"coding": [{"display": "K+"}]}) == "K+"
        assert assist.codeable_text({"coding": [{"code": "2823-3"}]}) == "2823-3"
        assert assist.codeable_text(None) is None
        assert assist.codeable_text("string") is None

    def test_medication_reference_display_is_used(self):
        med = {
            "resourceType": "MedicationRequest",
            "id": "m1",
            "medicationReference": {"display": "Amoxicillin 500mg"},
            "status": "active",
        }
        answer = assist.answer_question("medications?", assist.Evidence(medications=[med]))
        assert "Amoxicillin 500mg" in answer.findings[0].text


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


class TestRefusal:
    def test_an_unrecognised_question_is_refused_with_guidance(self):
        answer = assist.answer_question("what is the wifi password?", assist.Evidence())
        assert answer.answered is False
        assert answer.findings == ()
        assert any("not understood" in c for c in answer.caveats)
        assert any("Answerable topics" in c for c in answer.caveats)

    def test_refusal_lists_the_supported_topics(self):
        answer = assist.unsupported_answer("nonsense")
        blob = " ".join(answer.caveats)
        for topic in assist.SUPPORTED_TOPICS:
            assert topic in blob

    def test_every_answer_carries_the_disclaimer(self):
        for answer in (
            assist.answer_question("allergies?", assist.Evidence()),
            assist.unsupported_answer("x"),
            assist.advice_refusal(),
        ):
            assert "not a diagnosis" in answer.disclaimer


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def gateway(monkeypatch):
    import backend.services.gateway.main as gw

    bundles: dict[str, dict[str, Any]] = {
        "AllergyIntolerance": {
            "resourceType": "Bundle",
            "entry": [{"resource": allergy("a1", "Penicillin", "high", "anaphylaxis")}],
        },
        "Observation": {
            "resourceType": "Bundle",
            "entry": [{"resource": obs("o1", "Potassium", 5.4, "2026-07-20")}],
        },
        "MedicationRequest": {"resourceType": "Bundle", "entry": []},
        "Condition": {"resourceType": "Bundle", "entry": []},
        "Encounter": {"resourceType": "Bundle", "entry": []},
        "Patient": {"resourceType": "Bundle", "entry": []},
    }
    calls: list[str] = []

    async def fake_search(resource, params=None):
        calls.append(resource)
        return bundles.get(resource, {"resourceType": "Bundle", "entry": []})

    async def fake_read(resource, resource_id):
        return {"resourceType": resource, "id": resource_id}

    monkeypatch.setattr(gw.fhir, "search", fake_search)
    monkeypatch.setattr(gw.fhir, "read", fake_read)

    async def no_cache(*a, **k):
        return None

    monkeypatch.setattr(gw, "get_cached", no_cache)
    monkeypatch.setattr(gw, "set_cached", no_cache)

    with TestClient(gw.app, raise_server_exceptions=False) as client:
        yield client, calls, bundles


def auth(roles=("clinician",)):
    return {"Authorization": f"Bearer {create_access_token('dr.ask', roles=list(roles))}"}


class TestEndpoint:
    def test_requires_authentication(self, gateway):
        client, _, _ = gateway
        r = client.post("/assist/ask", json={"patient_id": "MRN-1", "question": "allergies?"})
        assert r.status_code == 401

    def test_a_viewer_is_refused(self, gateway):
        client, _, _ = gateway
        r = client.post(
            "/assist/ask",
            json={"patient_id": "MRN-1", "question": "allergies?"},
            headers=auth(["viewer"]),
        )
        assert r.status_code == 403

    def test_a_clinician_gets_a_cited_answer(self, gateway):
        client, _, _ = gateway
        r = client.post(
            "/assist/ask",
            json={"patient_id": "MRN-1", "question": "what allergies are recorded?"},
            headers=auth(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["answered"] is True
        assert body["findings"][0]["citations"][0]["resource_id"] == "a1"
        assert "Penicillin" in body["findings"][0]["text"]

    def test_only_the_relevant_resources_are_fetched(self, gateway):
        """A narrow question must not pull the whole chart: that would turn
        one question into a far broader disclosure."""
        client, calls, _ = gateway
        client.post(
            "/assist/ask",
            json={"patient_id": "MRN-1", "question": "any allergies?"},
            headers=auth(),
        )
        assert calls == ["AllergyIntolerance"]

    def test_the_response_reports_what_was_looked_at(self, gateway):
        client, _, _ = gateway
        body = client.post(
            "/assist/ask",
            json={"patient_id": "MRN-1", "question": "allergies?"},
            headers=auth(),
        ).json()
        assert body["retrieved"]["allergies"] == 1
        assert body["retrieved"]["failed"] == []

    def test_an_upstream_failure_is_reported_not_hidden(self, gateway, monkeypatch):
        import backend.services.gateway.main as gw

        async def boom(resource, params=None):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(gw.fhir, "search", boom)
        client, _, _ = gateway
        body = client.post(
            "/assist/ask",
            json={"patient_id": "MRN-1", "question": "allergies?"},
            headers=auth(),
        ).json()
        assert body["answered"] is False
        assert "allergies" in body["retrieved"]["failed"]
        assert any("could not be retrieved" in c for c in body["caveats"])

    def test_an_advice_question_is_refused_without_retrieval(self, gateway):
        """Refusing *before* fetching means an out-of-scope question does not
        even become a disclosure."""
        client, calls, _ = gateway
        body = client.post(
            "/assist/ask",
            json={"patient_id": "MRN-1", "question": "should I give penicillin?"},
            headers=auth(),
        ).json()
        assert body["answered"] is False
        assert calls == []
        assert any("does not give clinical advice" in c for c in body["caveats"])

    def test_an_unrecognised_question_fetches_nothing(self, gateway):
        client, calls, _ = gateway
        body = client.post(
            "/assist/ask",
            json={"patient_id": "MRN-1", "question": "what is the wifi password"},
            headers=auth(),
        ).json()
        assert body["answered"] is False
        assert calls == []

    def test_a_malformed_patient_id_is_rejected(self, gateway):
        client, _, _ = gateway
        r = client.post(
            "/assist/ask",
            json={"patient_id": "../etc/passwd", "question": "allergies?"},
            headers=auth(),
        )
        assert r.status_code == 400

    def test_an_overlong_question_is_rejected(self, gateway):
        client, _, _ = gateway
        r = client.post(
            "/assist/ask",
            json={"patient_id": "MRN-1", "question": "x" * 5000},
            headers=auth(),
        )
        assert r.status_code == 422

    def test_the_endpoint_never_writes(self, gateway, monkeypatch):
        """Read-only by construction: assert no FHIR create can be reached."""
        import backend.services.gateway.main as gw

        async def forbidden(*a, **k):
            raise AssertionError("assist attempted a write")

        monkeypatch.setattr(gw.fhir, "create", forbidden)
        client, _, _ = gateway
        r = client.post(
            "/assist/ask",
            json={"patient_id": "MRN-1", "question": "allergies and medications"},
            headers=auth(),
        )
        assert r.status_code == 200

    def test_capabilities_are_published_honestly(self, gateway):
        client, _, _ = gateway
        body = client.get("/assist/capabilities", headers=auth()).json()
        assert body["writes"] is False
        assert body["gives_advice"] is False
        # Honesty about what this is: not a model, and it must not claim to be.
        assert body["model_backed"] is False


class TestEndpointIsAudited:
    def test_the_question_itself_is_never_logged(self, gateway, caplog):
        """A clinician's free-text question can contain PHI."""
        client, _, _ = gateway
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.post(
                "/assist/ask",
                json={
                    "patient_id": "MRN-1",
                    "question": "allergies for SECRET-PATIENT-DETAIL",
                },
                headers=auth(),
            )
        assert "SECRET-PATIENT-DETAIL" not in "\n".join(
            r.getMessage() for r in caplog.records
        )

    def _records(self, caplog) -> list[dict[str, Any]]:
        return [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == "medicore.audit" and r.getMessage().startswith("{")
        ]

    def test_the_question_names_the_patient_in_the_audit_record(self, gateway, caplog):
        """Regression: the patient is in the request *body*, and the audit
        middleware only inspects the path and query string. Without an
        explicit annotation the trail recorded that someone used the
        assistant, but not whose chart they asked about."""
        from backend.common.middleware import audit_reference

        client, _, _ = gateway
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.post(
                "/assist/ask",
                json={"patient_id": "MRN-1", "question": "allergies?"},
                headers=auth(),
            )
        assist_rows = [r for r in self._records(caplog) if r["path"] == "/assist/ask"]
        assert assist_rows
        assert assist_rows[-1]["patient_ref"] == audit_reference("MRN-1")

    def test_a_refused_question_still_names_the_patient(self, gateway, caplog):
        """Someone probing charts they have no reason to open would otherwise
        leave no attributable trace, since a refusal fetches nothing."""
        from backend.common.middleware import audit_reference

        client, _, _ = gateway
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.post(
                "/assist/ask",
                json={"patient_id": "MRN-9", "question": "should I discharge them?"},
                headers=auth(),
            )
        assist_rows = [r for r in self._records(caplog) if r["path"] == "/assist/ask"]
        assert assist_rows[-1]["patient_ref"] == audit_reference("MRN-9")

    def test_a_failed_retrieval_still_names_the_patient(self, gateway, monkeypatch, caplog):
        """The access attempt happened even though the data did not arrive."""
        import backend.services.gateway.main as gw
        from backend.common.middleware import audit_reference

        async def boom(resource, params=None):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(gw.fhir, "search", boom)
        client, _, _ = gateway
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.post(
                "/assist/ask",
                json={"patient_id": "MRN-1", "question": "allergies?"},
                headers=auth(),
            )
        assist_rows = [r for r in self._records(caplog) if r["path"] == "/assist/ask"]
        assert assist_rows[-1]["patient_ref"] == audit_reference("MRN-1")

    def test_the_underlying_retrieval_is_audited_against_the_patient(self, gateway, caplog):
        """The assistant reads a chart, so it must appear in that patient's
        trail like any other access."""
        from backend.common.middleware import audit_reference

        client, _, _ = gateway
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.post(
                "/assist/ask",
                json={"patient_id": "MRN-1", "question": "allergies?"},
                headers=auth(),
            )
        records = [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == "medicore.audit" and r.getMessage().startswith("{")
        ]
        assert any(
            r.get("patient_ref") == audit_reference("MRN-1") for r in records
        ), "assistant retrieval was not attributed to the patient"
