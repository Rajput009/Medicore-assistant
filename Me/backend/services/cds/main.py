"""MediCore clinical decision support.

Exposes NEWS2 (National Early Warning Score 2) deterioration scoring. The
implementation follows the published Royal College of Physicians standard, so
results are explainable and auditable rather than derived from an ad-hoc
formula.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends
from pydantic import BaseModel, Field

from backend.common.app import create_service_app
from backend.common.config import settings
from backend.common.deps import Principal, clinical_staff

from .scoring import (
    ConsciousnessLevel,
    calculate_news2,
    legacy_band,
    normalised_score,
)

app = create_service_app(
    title="MediCore CDS",
    service_name="cds",
    version="1.0.0",
)

ClinicalUser = Annotated[Principal, Depends(clinical_staff)]


class Health(BaseModel):
    status: str
    service: str
    env: str


@app.get("/health", response_model=Health, tags=["ops"])
def health() -> Health:
    """Unauthenticated: probes and load balancers need this."""
    return Health(status="ok", service="cds", env=settings.env)


@app.get("/ready", tags=["ops"])
def ready() -> dict[str, str]:
    """CDS holds no state, so readiness matches liveness."""
    return {"status": "ok"}


# --------------------------------------------------------------------------
# NEWS2
# --------------------------------------------------------------------------


class VitalSigns(BaseModel):
    """Bounds are physiological limits; values outside them indicate a
    measurement or transcription error rather than a real observation."""

    respiratory_rate: float = Field(
        ..., gt=0, le=80, description="Breaths per minute"
    )
    spo2: float = Field(..., ge=50, le=100, description="Oxygen saturation (%)")
    temperature: float = Field(..., ge=25, le=45, description="Degrees Celsius")
    systolic_bp: float = Field(..., gt=0, le=300, description="mmHg")
    pulse: float = Field(..., gt=0, le=300, description="Beats per minute")
    consciousness: ConsciousnessLevel = Field(
        default=ConsciousnessLevel.ALERT, description="ACVPU scale"
    )
    on_supplemental_oxygen: bool = False
    use_spo2_scale2: bool = Field(
        default=False,
        description=(
            "Use SpO2 Scale 2 for patients with hypercapnic respiratory "
            "failure (target saturation 88-92%)."
        ),
    )


class ParameterBreakdown(BaseModel):
    name: str
    value: float | str
    score: int
    rationale: str


class News2Response(BaseModel):
    score: int
    band: str
    red_flag: bool
    recommended_response: str
    monitoring_frequency: str
    parameters: list[ParameterBreakdown]
    disclaimer: str


DISCLAIMER = (
    "NEWS2 is a track-and-trigger aid, not a diagnosis. It is not validated "
    "for children or pregnancy. Follow local escalation policy and clinical "
    "judgement."
)


@app.post("/news2", response_model=News2Response, tags=["cds"])
def news2(vitals: VitalSigns, principal: ClinicalUser) -> News2Response:
    """Full NEWS2 assessment with a per-parameter breakdown."""
    result = calculate_news2(
        respiratory_rate=vitals.respiratory_rate,
        spo2=vitals.spo2,
        temperature=vitals.temperature,
        systolic_bp=vitals.systolic_bp,
        pulse=vitals.pulse,
        consciousness=vitals.consciousness,
        on_supplemental_oxygen=vitals.on_supplemental_oxygen,
        use_spo2_scale2=vitals.use_spo2_scale2,
    )
    return News2Response(
        score=result.total,
        band=result.band.value,
        red_flag=result.red_flag,
        recommended_response=result.recommended_response,
        monitoring_frequency=result.monitoring_frequency,
        parameters=[
            ParameterBreakdown(
                name=p.name, value=p.value, score=p.score, rationale=p.rationale
            )
            for p in result.parameters
        ],
        disclaimer=DISCLAIMER,
    )


# --------------------------------------------------------------------------
# Backwards-compatible endpoint
# --------------------------------------------------------------------------


class RiskRequest(BaseModel):
    hr: float = Field(..., gt=0, le=300, description="Heart rate (bpm)")
    sbp: float = Field(..., gt=0, le=300, description="Systolic BP (mmHg)")
    spo2: float = Field(..., gt=0, le=100, description="Oxygen saturation (%)")
    respiratory_rate: float | None = Field(default=None, gt=0, le=80)
    temperature: float | None = Field(default=None, ge=25, le=45)


class RiskResp(BaseModel):
    score: float
    class_label: str
    news2_score: int
    red_flag: bool
    recommended_response: str
    disclaimer: str


@app.post("/risk", response_model=RiskResp, tags=["cds"])
def risk(req: RiskRequest, principal: ClinicalUser) -> RiskResp:
    """Simplified risk endpoint, now backed by NEWS2.

    Kept for existing clients. Respiratory rate and temperature carry real
    NEWS2 weight; when a caller omits them, normal values are assumed and the
    score is therefore a floor, not a complete assessment.
    """
    result = calculate_news2(
        respiratory_rate=req.respiratory_rate if req.respiratory_rate is not None else 16,
        spo2=req.spo2,
        temperature=req.temperature if req.temperature is not None else 37.0,
        systolic_bp=req.sbp,
        pulse=req.hr,
    )
    return RiskResp(
        # Normalised 0..1 so existing progress-style displays keep working.
        score=round(normalised_score(result.total), 3),
        class_label=legacy_band(result.total, result.red_flag),
        news2_score=result.total,
        red_flag=result.red_flag,
        recommended_response=result.recommended_response,
        disclaimer=DISCLAIMER,
    )


@app.get("/news2/reference", tags=["cds"])
def reference() -> dict[str, Any]:
    """Publish the thresholds so clients can explain a score to clinicians."""
    return {
        "standard": "NEWS2 (Royal College of Physicians, 2017)",
        "bands": {
            "0": "Low risk - routine monitoring",
            "1-4": "Low-medium - registered nurse review",
            "5-6 or any single parameter scoring 3": "Medium - urgent clinician review",
            ">=7": "High - emergency critical care assessment",
        },
        "disclaimer": DISCLAIMER,
    }
