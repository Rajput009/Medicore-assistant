from fastapi import FastAPI, Depends
from pydantic import BaseModel
from backend.common.config import settings
from backend.common.middleware import AuditLogMiddleware

from backend.common.telemetry import instrument_fastapi
app = instrument_fastapi(FastAPI(title="MediCore Patient Flow", version="0.1.0"), service_name="patient-flow")
app.add_middleware(AuditLogMiddleware)

class Health(BaseModel):
    status: str
    service: str
    env: str

@app.get("/health", response_model=Health)
def health():
    return Health(status="ok", service="patient-flow", env=settings.env)


from pydantic import BaseModel
from typing import List
from uuid import uuid4

class Bed(BaseModel):
    id: str
    ward: str
    occupied: bool

# in-memory stub
BEDS = [Bed(id=str(uuid4()), ward="A", occupied=False) for _ in range(4)]

@app.get("/beds", response_model=List[Bed])
def list_beds():
    return BEDS


from typing import Optional
from pydantic import BaseModel
from pymongo import MongoClient
from backend.common.config import settings

client = MongoClient(settings.mongo_uri)
mdb = client[settings.mongo_db]

class QueueItem(BaseModel):
    patient_id: str
    acuity: int
    dept: str

@app.post("/queue")
def enqueue(item: QueueItem):
    rec = item.model_dump()
    mdb.triage_queue.insert_one(rec)
    return {"ok": True}

@app.get("/queue")
def list_queue(limit: int = 10):
    items = list(mdb.triage_queue.find({}, {"_id": 0}).limit(limit))
    return {"items": items}
