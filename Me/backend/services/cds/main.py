"""MediCore clinical decision support (stub scoring)."""

from fastapi import FastAPI
from pydantic import BaseModel, Field

from backend.common.config import settings
from backend.common.middleware import AuditLogMiddleware
from backend.common.telemetry import instrument_fastapi

app = instrument_fastapi(
    FastAPI(title="MediCore CDS", version="0.1.0"), service_name="cds"
)
app.add_middleware(AuditLogMiddleware)


class Health(BaseModel):
    status: str
    service: str
    env: str


@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", service="cds", env=settings.env)


class RiskRequest(BaseModel):
    """Vitals. Bounds reject physiologically impossible input (e.g. SpO2 of 400)
    that would otherwise silently produce a meaningless score."""

    hr: float = Field(..., gt=0, le=300, description="Heart rate (bpm)")
    sbp: float = Field(..., gt=0, le=300, description="Systolic BP (mmHg)")
    spo2: float = Field(..., gt=0, le=100, description="Oxygen saturation (%)")


class RiskResp(BaseModel):
    score: float
    class_label: str


@app.post("/risk", response_model=RiskResp)
def risk(req: RiskRequest) -> RiskResp:
    """Dummy scoring logic — replace with a validated clinical model.

    Each term is clamped at 0 so that a *better* vital can never offset a worse
    one and pull a genuinely sick patient's score down (the original formula
    let high blood pressure cancel out tachycardia).
    """
    hr_term = max(req.hr - 100.0, 0.0) / 100.0  # tachycardia
    sbp_term = max(90.0 - req.sbp, 0.0) / 90.0  # hypotension
    spo2_term = max(95.0 - req.spo2, 0.0) / 95.0  # hypoxia

    score = min(hr_term + sbp_term + spo2_term, 1.0)
    label = "high" if score > 0.8 else "medium" if score > 0.4 else "low"
    return RiskResp(score=round(score, 3), class_label=label)
