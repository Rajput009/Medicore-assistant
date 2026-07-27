"""Build FHIR R4 Observation resources from recorded vital signs.

NEWS2 inputs are only useful longitudinally if they are persisted somewhere
the rest of the hospital can read. That means writing real Observations with
proper LOINC codes and UCUM units — not a private table — so the values show
up in the EHR's own flowsheets and in any other system reading FHIR.

Codes are LOINC (the FHIR-required vital-signs code system) and units are
UCUM, both as mandated by the FHIR "vitalsigns" profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
OBS_CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"


@dataclass(frozen=True)
class VitalDefinition:
    """One vital sign: how to code it and what unit it carries."""

    key: str
    loinc_code: str
    display: str
    unit: str
    ucum_code: str
    minimum: float
    maximum: float


# The six NEWS2 parameters that carry a numeric value. Consciousness (ACVPU)
# is coded separately below because it is not a quantity.
VITAL_DEFINITIONS: tuple[VitalDefinition, ...] = (
    VitalDefinition("respiratory_rate", "9279-1", "Respiratory rate", "breaths/minute", "/min", 1, 80),
    VitalDefinition("spo2", "59408-5", "Oxygen saturation in Arterial blood by Pulse oximetry", "%", "%", 1, 100),
    VitalDefinition("temperature", "8310-5", "Body temperature", "Cel", "Cel", 25, 45),
    VitalDefinition("systolic_bp", "8480-6", "Systolic blood pressure", "mmHg", "mm[Hg]", 1, 300),
    VitalDefinition("pulse", "8867-4", "Heart rate", "beats/minute", "/min", 1, 300),
)

VITALS_BY_KEY: dict[str, VitalDefinition] = {v.key: v for v in VITAL_DEFINITIONS}

# ACVPU level of consciousness. 6234-8 is the LOINC "Level of consciousness"
# concept; the value is a plain string so it stays readable in any viewer.
CONSCIOUSNESS_LOINC = "6234-8"
CONSCIOUSNESS_DISPLAY = "Level of consciousness"
CONSCIOUSNESS_LABELS = {
    "A": "Alert",
    "C": "Confusion",
    "V": "Voice",
    "P": "Pain",
    "U": "Unresponsive",
}

# NEWS2 aggregate score. able to be trended alongside the raw vitals.
NEWS2_LOINC = "not-applicable"
NEWS2_DISPLAY = "NEWS2 total score"


def _now_iso() -> str:
    # Second precision: sub-second noise is meaningless for a bedside
    # observation and makes values harder to read in a flowsheet.
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _base_observation(
    patient_id: str,
    effective: str,
    encounter_id: str | None,
    performer: str | None,
) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "Observation",
        # "final" not "preliminary": a clinician has entered and submitted
        # this reading, so it is a completed observation.
        "status": "final",
        "category": [
            {
                "coding": [
                    {
                        "system": OBS_CATEGORY_SYSTEM,
                        "code": "vital-signs",
                        "display": "Vital Signs",
                    }
                ]
            }
        ],
        "subject": {"reference": f"Patient/{patient_id}"},
        "effectiveDateTime": effective,
    }
    if encounter_id:
        # Ties the reading to "this visit" rather than just the MRN.
        resource["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    if performer:
        # Who recorded it — required for a defensible clinical record.
        resource["performer"] = [{"display": performer}]
    return resource


def build_vital_observation(
    definition: VitalDefinition,
    value: float,
    patient_id: str,
    *,
    effective: str | None = None,
    encounter_id: str | None = None,
    performer: str | None = None,
) -> dict[str, Any]:
    """One numeric vital sign as a FHIR Observation."""
    resource = _base_observation(
        patient_id, effective or _now_iso(), encounter_id, performer
    )
    resource["code"] = {
        "coding": [
            {
                "system": LOINC,
                "code": definition.loinc_code,
                "display": definition.display,
            }
        ],
        "text": definition.display,
    }
    resource["valueQuantity"] = {
        "value": value,
        "unit": definition.unit,
        "system": UCUM,
        "code": definition.ucum_code,
    }
    return resource


def build_consciousness_observation(
    level: str,
    patient_id: str,
    *,
    effective: str | None = None,
    encounter_id: str | None = None,
    performer: str | None = None,
) -> dict[str, Any]:
    """ACVPU consciousness level as a coded (non-quantity) Observation."""
    resource = _base_observation(
        patient_id, effective or _now_iso(), encounter_id, performer
    )
    resource["code"] = {
        "coding": [
            {
                "system": LOINC,
                "code": CONSCIOUSNESS_LOINC,
                "display": CONSCIOUSNESS_DISPLAY,
            }
        ],
        "text": CONSCIOUSNESS_DISPLAY,
    }
    resource["valueString"] = CONSCIOUSNESS_LABELS.get(level, level)
    return resource


def build_news2_observation(
    score: int,
    patient_id: str,
    *,
    effective: str | None = None,
    encounter_id: str | None = None,
    performer: str | None = None,
) -> dict[str, Any]:
    """The NEWS2 aggregate as a survey Observation.

    Recorded under the ``survey`` category, not ``vital-signs``: the total is
    a derived assessment score, and filing it as a vital sign would corrupt
    flowsheets that expect only measured values there.
    """
    resource = _base_observation(
        patient_id, effective or _now_iso(), encounter_id, performer
    )
    resource["category"] = [
        {
            "coding": [
                {
                    "system": OBS_CATEGORY_SYSTEM,
                    "code": "survey",
                    "display": "Survey",
                }
            ]
        }
    ]
    resource["code"] = {"text": NEWS2_DISPLAY}
    resource["valueInteger"] = score
    return resource


def build_vitals_bundle(
    vitals: dict[str, float],
    patient_id: str,
    *,
    consciousness: str | None = None,
    news2_score: int | None = None,
    encounter_id: str | None = None,
    performer: str | None = None,
    effective: str | None = None,
) -> list[dict[str, Any]]:
    """Every Observation for one set of readings, sharing an effective time.

    A single timestamp for the whole set matters: it is what lets a flowsheet
    group these values into one column instead of scattering them across
    several near-identical times.
    """
    moment = effective or _now_iso()
    resources: list[dict[str, Any]] = []

    for key, value in vitals.items():
        definition = VITALS_BY_KEY.get(key)
        if definition is None:
            continue
        resources.append(
            build_vital_observation(
                definition,
                value,
                patient_id,
                effective=moment,
                encounter_id=encounter_id,
                performer=performer,
            )
        )

    if consciousness:
        resources.append(
            build_consciousness_observation(
                consciousness,
                patient_id,
                effective=moment,
                encounter_id=encounter_id,
                performer=performer,
            )
        )

    if news2_score is not None:
        resources.append(
            build_news2_observation(
                news2_score,
                patient_id,
                effective=moment,
                encounter_id=encounter_id,
                performer=performer,
            )
        )

    return resources
