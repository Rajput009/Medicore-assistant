from fastapi import FastAPI, Depends
from pydantic import BaseModel
from backend.common.config import settings
from backend.common.middleware import AuditLogMiddleware

from backend.common.telemetry import instrument_fastapi
app = instrument_fastapi(FastAPI(title="MediCore CDS", version="0.1.0"), service_name="cds")
app.add_middleware(AuditLogMiddleware)

class Health(BaseModel):
    status: str
    service: str
    env: str

@app.get("/health", response_model=Health)
def health():
    return Health(status="ok", service="cds", env=settings.env)


from pydantic import BaseModel

class RiskRequest(BaseModel):
    hr: float
    sbp: float
    spo2: float

class RiskResp(BaseModel):
    score: float
    class_label: str

@app.post("/risk", response_model=RiskResp)
def risk(req: RiskRequest):
    # Dummy scoring logic (replace with validated model)
    score = (req.hr/200) + (100-req.sbp)/200 + (100-req.spo2)/100
    label = "high" if score > 0.8 else "medium" if score > 0.4 else "low"
    return RiskResp(score=round(score,3), class_label=label)
