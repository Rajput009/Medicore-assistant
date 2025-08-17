from fastapi import Request, HTTPException, status
from fastapi import FastAPI, Depends
from pydantic import BaseModel
from backend.common.config import settings
from backend.common.middleware import AuditLogMiddleware

from backend.common.telemetry import instrument_fastapi
from backend.services.gateway.auth_middleware import JWTAuthMiddleware
app = instrument_fastapi(FastAPI(title="MediCore Gateway", version="0.1.0"), service_name="gateway")
app.add_middleware(AuditLogMiddleware)
app.add_middleware(JWTAuthMiddleware)

class Health(BaseModel):
    status: str
    service: str
    env: str

@app.get("/health", response_model=Health)
def health():
    return Health(status="ok", service="gateway", env=settings.env)


from fastapi import HTTPException
from backend.common.fhir_client import default_fhir_client
from backend.services.gateway.auth import get_current_user, requires_roles, User
fhir = default_fhir_client()

@app.get("/fhir/patient/{patient_id}")
def get_patient(patient_id: str):
    try:
        return fhir.get("Patient", patient_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


from fastapi import Request, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from backend.common.config import settings

security = HTTPBearer(auto_error=False)

def verify_jwt(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return payload

@app.get("/secure")
def secure_route(payload: dict = Depends(verify_jwt)):
    return {"ok": True, "sub": payload.get("sub"), "roles": payload.get("roles")}


from fastapi import Depends, HTTPException, status
# protected proxy
@app.get("/fhir/patient/{patient_id}/protected")
async def get_patient_protected(patient_id: str, user: User = Depends(get_current_user)):
    if not set(user.roles).intersection({'clinician','admin'}):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    try:
        return fhir.get("Patient", patient_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ----------------- Expanded FHIR Endpoints -----------------

@app.get("/fhir/patient/search")
async def search_patients(request: Request):
    user = getattr(request.state, "user", None)
    if not user or not set(user.get("roles", [])).intersection({"clinician","admin"}):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    params = dict(request.query_params)
    # try cache
    cached = await get_cached("Patient", params, max_age_seconds=300)
    if cached is not None:
        return cached
    try:
        resp = fhir.search("Patient", params)
        # store cache async
        await set_cached("Patient", params, resp)
        return resp
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/fhir/encounter/{encounter_id}")
async def get_encounter(encounter_id: str, request: Request):
    user = getattr(request.state, "user", None)
    if not user or not set(user.get("roles", [])).intersection({"clinician","admin"}):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    try:
        return fhir.get("Encounter", encounter_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/fhir/encounter/search")
async def search_encounters(request: Request):
    user = getattr(request.state, "user", None)
    if not user or not set(user.get("roles", [])).intersection({"clinician","admin"}):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    params = dict(request.query_params)
    cached = await get_cached("Encounter", params, max_age_seconds=300)
    if cached is not None:
        return cached
    try:
        resp = fhir.search("Encounter", params)
        await set_cached("Encounter", params, resp)
        return resp
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/fhir/observation/{obs_id}")
async def get_observation(obs_id: str, request: Request):
    user = getattr(request.state, "user", None)
    if not user or not set(user.get("roles", [])).intersection({"clinician","admin"}):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    try:
        return fhir.get("Observation", obs_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/fhir/observation/search")
async def search_observations(request: Request):
    user = getattr(request.state, "user", None)
    if not user or not set(user.get("roles", [])).intersection({"clinician","admin"}):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    params = dict(request.query_params)
    cached = await get_cached("Observation", params, max_age_seconds=60)
    if cached is not None:
        return cached
    try:
        resp = fhir.search("Observation", params)
        await set_cached("Observation", params, resp)
        return resp
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/fhir/medicationrequest/{med_id}")
async def get_medication_request(med_id: str, request: Request):
    user = getattr(request.state, "user", None)
    if not user or not set(user.get("roles", [])).intersection({"clinician","admin"}):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    try:
        return fhir.get("MedicationRequest", med_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/fhir/medicationrequest/search")
async def search_medication_requests(request: Request):
    user = getattr(request.state, "user", None)
    if not user or not set(user.get("roles", [])).intersection({"clinician","admin"}):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    params = dict(request.query_params)
    cached = await get_cached("MedicationRequest", params, max_age_seconds=300)
    if cached is not None:
        return cached
    try:
        resp = fhir.search("MedicationRequest", params)
        await set_cached("MedicationRequest", params, resp)
        return resp
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ----------------- Admin Cache Invalidation Endpoints -----------------

@app.delete("/cache/{resource}")
async def admin_invalidate_cache(resource: str, request: Request, patient: str = None):
    user = getattr(request.state, "user", None)
    if not user or "admin" not in user.get("roles", []):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    try:
        await invalidate_cache(resource, patient)
        return {"status": "ok", "message": f"Cache invalidated for {resource} (patient={patient})"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
