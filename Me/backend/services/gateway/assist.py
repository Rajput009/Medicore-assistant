"""Grounded chart Q&A — the retrieval and citation layer for clinical answers.

Tier 4 of the roadmap asks for "AI that earns the name: assistive, cited,
never autonomous". The hard part of that in a hospital is not calling a model.
It is everything around the call:

  * retrieving only what this caller is already authorised to see
  * citing a real resource for every claim
  * refusing when the evidence is absent, and saying *why*
  * never letting "we could not fetch it" render as "there is none"
  * never writing, ordering, or acting

This module is that layer. The default answerer is **extractive**: findings are
assembled from values copied out of retrieved FHIR resources, so it cannot
invent a number. That is a deliberate first implementation, not a placeholder
for one:

  * No LLM provider is configured in this deployment, and egress is
    default-deny to an allow-list of FHIR/IdP hosts. Routing PHI to a
    third-party model needs a signed BAA and an egress change — a
    procurement decision, not something to enable quietly in code.
  * An extractive answerer is verifiable. Every claim is traceable to a
    resource id, and the tests can assert exact clinical content.

``AnswerComposer`` is the seam. A hospital that signs a BAA can add a
model-backed composer, and :func:`validate_answer` still rejects any
uncited claim it produces — the guardrail lives in the framework, not in a
prompt, so swapping the implementation cannot weaken it.

Everything here is pure: no I/O, no FHIR client, no request object. The
gateway performs retrieval (already authorised, cached and audited) and hands
the resources in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# A question is free text typed by a clinician and may contain PHI, so it is
# bounded and never logged. Only the classified intent is recorded.
MAX_QUESTION_LENGTH = 300
MAX_FINDINGS = 12
MAX_CITATIONS_PER_FINDING = 6
# How many observations of one kind to summarise in a trend.
MAX_TREND_POINTS = 5

DISCLAIMER = (
    "Assembled from this patient's recorded data. It is an aid to reading the "
    "chart, not a diagnosis or a recommendation, and it may be incomplete. "
    "Verify against the source record before acting."
)


class UncitedClaimError(ValueError):
    """A finding was produced without evidence backing it.

    Raised rather than returned: an uncited clinical claim is the failure this
    whole module exists to prevent, so it must never reach a clinician.
    """


@dataclass(frozen=True)
class Citation:
    """A pointer to the resource a claim came from."""

    resource_type: str
    resource_id: str
    label: str
    recorded: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "label": self.label,
            "recorded": self.recorded,
        }


@dataclass(frozen=True)
class Finding:
    """One claim, and the evidence for it."""

    text: str
    citations: tuple[Citation, ...]
    # Surfaced first and badged: an allergy is not the same kind of fact as a
    # heart rate, and truncation must never drop it.
    critical: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "critical": self.critical,
            "citations": [c.as_dict() for c in self.citations],
        }


@dataclass(frozen=True)
class Answer:
    intents: tuple[str, ...]
    findings: tuple[Finding, ...]
    # Everything the answer does *not* establish. A caveat is as clinically
    # important as a finding: "the allergy list failed to load" changes what a
    # clinician should do next.
    caveats: tuple[str, ...]
    answered: bool
    disclaimer: str = DISCLAIMER

    def as_dict(self) -> dict[str, Any]:
        return {
            "intents": list(self.intents),
            "findings": [f.as_dict() for f in self.findings],
            "caveats": list(self.caveats),
            "answered": self.answered,
            "disclaimer": self.disclaimer,
        }


@dataclass
class Evidence:
    """Resources retrieved for the question, plus what could not be fetched.

    ``failed`` is the safety-critical field. A retrieval that errored is not
    an empty result: reporting "no allergies recorded" when the allergy search
    threw is the single most dangerous output this module could produce.
    """

    observations: list[dict[str, Any]] = field(default_factory=list)
    allergies: list[dict[str, Any]] = field(default_factory=list)
    medications: list[dict[str, Any]] = field(default_factory=list)
    conditions: list[dict[str, Any]] = field(default_factory=list)
    encounters: list[dict[str, Any]] = field(default_factory=list)
    failed: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Intent classification
#
# Deliberately keyword-based and deterministic. A clinician needs to be able
# to predict what the assistant will look at; a fuzzy classifier that silently
# answers a different question than the one asked is worse than one that
# refuses.
# ---------------------------------------------------------------------------

INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "allergies": ("allerg", "anaphyla", "reaction", "intoleran"),
    "medications": ("medication", "meds", " med ", "drug", "prescri", "taking", "dose"),
    "problems": ("problem", "condition", "diagnos", "history", "pmh", "comorbid"),
    "encounters": ("admit", "admission", "encounter", "visit", "why here", "presenting"),
    "observations": (
        "vital", "observation", "obs", "bp", "blood pressure", "heart rate",
        "pulse", "temperature", "temp", "sat", "spo2", "oxygen", "news2",
        "score", "result", "lab", "latest", "last", "trend", "level",
    ),
}

# Analytes and vitals worth filtering an observation search down to. Matching
# is on the resource's own display text, so an unlisted term still works — the
# list only helps the question mention it in a natural way.
_ANALYTE_HINTS = (
    "potassium", "sodium", "creatinine", "urea", "glucose", "lactate",
    "haemoglobin", "hemoglobin", "platelet", "crp", "troponin", "bilirubin",
    "heart rate", "pulse", "respiratory", "temperature", "oxygen",
    "blood pressure", "systolic", "diastolic", "saturation", "spo2", "news2",
)

SUPPORTED_TOPICS = (
    "allergies",
    "medications",
    "problems / diagnoses",
    "encounters / admissions",
    "observations, vitals and lab results",
)


def normalise_question(raw: str) -> str:
    """Trim, collapse whitespace and bound the question."""
    return " ".join(str(raw or "").split())[:MAX_QUESTION_LENGTH]


def classify(question: str) -> tuple[str, ...]:
    """Which topics the question touches. Empty means "not understood"."""
    text = f" {normalise_question(question).lower()} "
    hits = [
        intent
        for intent, keywords in INTENT_KEYWORDS.items()
        if any(k in text for k in keywords)
    ]
    # Naming a specific analyte *is* an observation question, even without a
    # keyword like "latest" or "result". Otherwise "potassium?" — the most
    # natural way a clinician asks — is refused as not understood.
    if "observations" not in hits and analyte_filter(question):
        hits.append("observations")
    # Stable, predictable ordering: safety-critical topics first.
    order = ("allergies", "medications", "problems", "observations", "encounters")
    return tuple(i for i in order if i in hits)


def analyte_filter(question: str) -> str | None:
    """The specific measurement a question asks about, if any.

    "What was the last potassium?" should not return every observation on the
    chart. Returns None when the question is a general one.
    """
    text = normalise_question(question).lower()
    for hint in _ANALYTE_HINTS:
        if hint in text:
            return hint
    return None


# ---------------------------------------------------------------------------
# Reading FHIR safely
#
# Every helper tolerates a malformed resource. An assistant that raises on an
# odd payload is an assistant that is unavailable exactly when a chart is
# messy, which is when it is most needed.
# ---------------------------------------------------------------------------


def codeable_text(value: Any) -> str | None:
    """Human label from any of the shapes FHIR allows for a coded concept."""
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    for entry in value.get("coding") or []:
        if isinstance(entry, dict):
            display = entry.get("display")
            if isinstance(display, str) and display.strip():
                return display.strip()
    for entry in value.get("coding") or []:
        if isinstance(entry, dict):
            code = entry.get("code")
            if isinstance(code, str) and code.strip():
                return code.strip()
    return None


def resource_label(resource: dict[str, Any]) -> str:
    """Best available human name for a resource."""
    if not isinstance(resource, dict):
        return "record"
    for key in ("code", "medicationCodeableConcept", "type"):
        label = codeable_text(resource.get(key))
        if label:
            return label
    # MedicationRequest may reference rather than inline the drug.
    reference = resource.get("medicationReference")
    if isinstance(reference, dict):
        display = reference.get("display")
        if isinstance(display, str) and display.strip():
            return display.strip()
    for key in ("class",):
        label = codeable_text(resource.get(key))
        if label:
            return label
    return str(resource.get("resourceType") or "record")


def effective_time(resource: dict[str, Any]) -> str | None:
    """When the resource was recorded, across the fields FHIR uses."""
    if not isinstance(resource, dict):
        return None
    for key in ("effectiveDateTime", "issued", "recordedDate", "authoredOn", "onsetDateTime"):
        value = resource.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("effectivePeriod", "period"):
        node = resource.get(key)
        if isinstance(node, dict):
            for edge in ("end", "start"):
                value = node.get(edge)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def observation_value(resource: dict[str, Any]) -> str | None:
    """Render an observation's value with units, without interpreting it."""
    if not isinstance(resource, dict):
        return None
    quantity = resource.get("valueQuantity")
    if isinstance(quantity, dict):
        value = quantity.get("value")
        if isinstance(value, (int, float)):
            unit = quantity.get("unit") or quantity.get("code")
            return f"{value} {unit}".strip() if isinstance(unit, str) else str(value)
    for key in ("valueInteger", "valueDecimal"):
        value = resource.get(key)
        if isinstance(value, (int, float)):
            return str(value)
    text = resource.get("valueString")
    if isinstance(text, str) and text.strip():
        return text.strip()[:80]
    concept = codeable_text(resource.get("valueCodeableConcept"))
    if concept:
        return concept
    return None


def _status(resource: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = resource.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        label = codeable_text(value)
        if label:
            return label.lower()
    return None


def _cite(resource: dict[str, Any], fallback_type: str) -> Citation:
    return Citation(
        resource_type=str(resource.get("resourceType") or fallback_type),
        resource_id=str(resource.get("id") or "unknown"),
        label=resource_label(resource),
        recorded=effective_time(resource),
    )


def _sort_newest_first(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first; undated resources sort last rather than being dropped."""
    return sorted(
        [r for r in resources if isinstance(r, dict)],
        key=lambda r: (effective_time(r) or ""),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Composers
# ---------------------------------------------------------------------------


class AnswerComposer(Protocol):
    """Turns retrieved evidence into findings.

    A model-backed implementation would satisfy this too, and would be held to
    the same contract by :func:`validate_answer` — every finding cited, no
    exceptions.
    """

    def compose(self, question: str, intents: tuple[str, ...], evidence: Evidence) -> Answer:
        ...  # pragma: no cover - protocol


def _allergy_findings(evidence: Evidence) -> tuple[list[Finding], list[str]]:
    """Allergies, with the one rule that matters most.

    "We could not load the allergy list" and "this patient has no known
    allergies" must never render the same way. The first is a reason to go and
    look; the second is a clinical statement. Conflating them is how someone
    gets given a drug that kills them.
    """
    caveats: list[str] = []
    if "allergies" in evidence.failed:
        caveats.append(
            "Allergy list could not be retrieved — this is NOT a statement that "
            "the patient has no allergies. Check the source record before prescribing."
        )
        return [], caveats

    if not evidence.allergies:
        caveats.append(
            "No allergy records were found. Absence of a record is not the same "
            "as confirmed 'no known allergies'; confirm with the patient."
        )
        return [], caveats

    findings: list[Finding] = []
    for resource in _sort_newest_first(evidence.allergies):
        label = resource_label(resource)
        criticality = _status(resource, "criticality") or ""
        clinical = _status(resource, "clinicalStatus") or "unknown status"
        reactions: list[str] = []
        for reaction in resource.get("reaction") or []:
            if not isinstance(reaction, dict):
                continue
            for manifestation in reaction.get("manifestation") or []:
                text = codeable_text(manifestation)
                if text:
                    reactions.append(text)
        detail = f" — reaction: {', '.join(reactions[:3])}" if reactions else ""
        severity = " (high criticality)" if criticality.startswith("high") else ""
        findings.append(
            Finding(
                text=f"Allergy: {label}{severity} [{clinical}]{detail}",
                citations=(_cite(resource, "AllergyIntolerance"),),
                critical=criticality.startswith("high"),
            )
        )
    return findings, caveats


def _simple_list_findings(
    resources: list[dict[str, Any]],
    *,
    failed: bool,
    noun: str,
    prefix: str,
    fallback_type: str,
    status_keys: tuple[str, ...],
) -> tuple[list[Finding], list[str]]:
    caveats: list[str] = []
    if failed:
        caveats.append(f"{noun.capitalize()} could not be retrieved.")
        return [], caveats
    if not resources:
        caveats.append(f"No {noun} recorded for this patient.")
        return [], caveats

    findings: list[Finding] = []
    for resource in _sort_newest_first(resources):
        status = _status(resource, *status_keys)
        suffix = f" [{status}]" if status else ""
        findings.append(
            Finding(
                text=f"{prefix}: {resource_label(resource)}{suffix}",
                citations=(_cite(resource, fallback_type),),
            )
        )
    return findings, caveats


def _observation_findings(
    question: str, evidence: Evidence
) -> tuple[list[Finding], list[str]]:
    caveats: list[str] = []
    if "observations" in evidence.failed:
        caveats.append("Observations could not be retrieved.")
        return [], caveats

    resources = _sort_newest_first(evidence.observations)
    wanted = analyte_filter(question)
    if wanted:
        matched = [r for r in resources if wanted in resource_label(r).lower()]
        if not matched:
            caveats.append(
                f"No recorded observation matching '{wanted}' was found. "
                "It may exist under a different name, or not have been taken."
            )
            return [], caveats
        resources = matched

    if not resources:
        caveats.append("No observations recorded for this patient.")
        return [], caveats

    # Group by measurement so "latest potassium" reads as one answer with a
    # trend, rather than five unrelated rows.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for resource in resources:
        grouped.setdefault(resource_label(resource), []).append(resource)

    findings: list[Finding] = []
    for label, series in grouped.items():
        latest = series[0]
        value = observation_value(latest)
        if value is None:
            # A resource we cannot read a value from is reported as such rather
            # than skipped: a silently missing result reads as "not measured".
            findings.append(
                Finding(
                    text=f"{label}: recorded, but the value could not be read",
                    citations=(_cite(latest, "Observation"),),
                )
            )
            continue
        when = effective_time(latest)
        text = f"Latest {label}: {value}" + (f" ({when})" if when else "")
        earlier = [
            f"{observation_value(r)}"
            for r in series[1:MAX_TREND_POINTS]
            if observation_value(r) is not None
        ]
        if earlier:
            text += f"; previous: {', '.join(earlier)}"
        findings.append(
            Finding(
                text=text,
                citations=tuple(
                    _cite(r, "Observation") for r in series[:MAX_CITATIONS_PER_FINDING]
                ),
            )
        )
    return findings, caveats


class ExtractiveComposer:
    """Assembles findings by copying values out of retrieved resources.

    Cannot hallucinate: every string it emits is either a fixed template or a
    value read from a resource that is cited alongside it.
    """

    def compose(self, question: str, intents: tuple[str, ...], evidence: Evidence) -> Answer:
        findings: list[Finding] = []
        caveats: list[str] = []

        if "allergies" in intents:
            found, notes = _allergy_findings(evidence)
            findings += found
            caveats += notes

        if "medications" in intents:
            found, notes = _simple_list_findings(
                evidence.medications,
                failed="medications" in evidence.failed,
                noun="medications",
                prefix="Medication",
                fallback_type="MedicationRequest",
                status_keys=("status",),
            )
            findings += found
            caveats += notes

        if "problems" in intents:
            found, notes = _simple_list_findings(
                evidence.conditions,
                failed="conditions" in evidence.failed,
                noun="problems",
                prefix="Problem",
                fallback_type="Condition",
                status_keys=("clinicalStatus", "verificationStatus"),
            )
            findings += found
            caveats += notes

        if "observations" in intents:
            found, notes = _observation_findings(question, evidence)
            findings += found
            caveats += notes

        if "encounters" in intents:
            found, notes = _simple_list_findings(
                evidence.encounters,
                failed="encounters" in evidence.failed,
                noun="encounters",
                prefix="Encounter",
                fallback_type="Encounter",
                status_keys=("status",),
            )
            findings += found
            caveats += notes

        # Critical findings survive truncation: an allergy must not be the row
        # that falls off the end of the list.
        findings.sort(key=lambda f: not f.critical)
        if len(findings) > MAX_FINDINGS:
            caveats.append(
                f"Showing {MAX_FINDINGS} of {len(findings)} findings; "
                "open the chart for the full record."
            )
            findings = findings[:MAX_FINDINGS]

        return Answer(
            intents=intents,
            findings=tuple(findings),
            caveats=tuple(caveats),
            answered=bool(findings),
        )


def unsupported_answer(question: str) -> Answer:
    """The refusal path.

    Saying "I cannot answer that, here is what I can answer" is a better
    outcome than guessing at the intent and confidently answering a different
    question than the one asked.
    """
    return Answer(
        intents=(),
        findings=(),
        caveats=(
            "This question was not understood, so nothing was looked up.",
            "Answerable topics: " + "; ".join(SUPPORTED_TOPICS) + ".",
        ),
        answered=False,
    )


# ---------------------------------------------------------------------------
# The guardrail
# ---------------------------------------------------------------------------


def validate_answer(answer: Answer) -> Answer:
    """Reject any claim that is not backed by evidence.

    Applied to *every* composer's output, including a future model-backed one.
    That is the point: the safety property is enforced by the framework rather
    than requested in a prompt, so it cannot be talked out of.
    """
    for finding in answer.findings:
        if not finding.citations:
            raise UncitedClaimError(
                f"finding without a citation: {finding.text[:60]!r}"
            )
        for citation in finding.citations:
            if not citation.resource_id or not citation.resource_type:
                raise UncitedClaimError(
                    f"citation missing a resource reference: {finding.text[:60]!r}"
                )
    if answer.answered and not answer.findings:
        raise UncitedClaimError("answer claims to be answered but has no findings")
    return answer


def answer_question(
    question: str,
    evidence: Evidence,
    composer: AnswerComposer | None = None,
) -> Answer:
    """Classify, compose and validate. The single entry point.

    A caller-supplied composer is always run and always validated. An earlier
    version returned the refusal before invoking it when the question did not
    classify, which meant a composer could not be exercised on that path —
    the guardrail looked universal but had a hole in it.
    """
    cleaned = normalise_question(question)
    intents = classify(cleaned)
    # The default composer has nothing to work from without an intent, so it
    # short-circuits to the refusal. An explicit composer always gets its turn
    # — it may understand questions the keyword classifier cannot — and its
    # output is validated either way.
    if not intents and composer is None:
        return validate_answer(unsupported_answer(cleaned))
    engine = composer or ExtractiveComposer()
    return validate_answer(engine.compose(cleaned, intents, evidence))


# Terms whose presence means the question is asking the assistant to decide
# something, rather than to read the chart back.
_ADVICE_PATTERNS = re.compile(
    r"\b(should i|what should|do i (?:give|prescribe|start|stop)|"
    r"recommend|advise|diagnos(?:e|is) (?:this|the)|treat(?:ment)? plan|"
    r"is it safe to|can i (?:give|discharge|stop))\b",
    re.IGNORECASE,
)


def requests_advice(question: str) -> bool:
    """True when the question asks for a decision rather than a fact.

    The assistant reads the chart back with citations; it does not advise. A
    question like "should I give amoxicillin?" gets an explicit refusal, not a
    medication list that could be mistaken for endorsement.
    """
    return bool(_ADVICE_PATTERNS.search(normalise_question(question)))


def advice_refusal() -> Answer:
    return Answer(
        intents=(),
        findings=(),
        caveats=(
            "This assistant reports recorded data; it does not give clinical "
            "advice or recommend treatment.",
            "Rephrase as a question about the record, e.g. 'what allergies are "
            "recorded?' or 'what was the last potassium?'.",
        ),
        answered=False,
    )
