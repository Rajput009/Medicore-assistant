"""MediCore API gateway.

Edge service: enforces JWT auth + RBAC and proxies FHIR reads/searches with a
Postgres-backed response cache.
"""

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from backend.common.cache import (
    close_pool,
    get_cached,
    init_pool,
    invalidate_cache,
    set_cached,
    start_janitor,
    stop_janitor,
)
from backend.common.cache import (
    ping as cache_ping,
)
from backend.common.config import settings
from backend.common.fhir_client import FHIRError, default_fhir_client
from backend.common.logging import configure_logging
from backend.common.middleware import AuditLogMiddleware
from backend.common.telemetry import instrument_fastapi
from backend.services.gateway.auth import User, admin_only, clinician_or_admin
from backend.services.gateway.auth_middleware import JWTAuthMiddleware

fhir = default_fhir_client()

# Cache TTLs per resource type (seconds). Observations change far more often.
CACHE_TTL = {
    "Patient": 300,
    "Encounter": 300,
    "Observation": 60,
    "MedicationRequest": 300,
}

# Only these resource types may be targeted by cache invalidation, so a path
# parameter can never be used to probe arbitrary values.
KNOWN_RESOURCES = frozenset(CACHE_TTL)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level, service="gateway")
    try:
        # Create the pool (and cache table) up front so the first request does
        # not pay the setup cost, and readiness reflects reality immediately.
        await init_pool()
        await start_janitor()
    except Exception:
        logger.exception("cache initialisation failed; continuing degraded")
    try:
        yield
    finally:
        await stop_janitor()
        await fhir.aclose()
        await close_pool()


app = instrument_fastapi(
    FastAPI(title="MediCore Gateway", version="0.1.0", lifespan=lifespan),
    service_name="gateway",
)

# Middleware runs in reverse registration order, so registering the auth
# middleware first makes the audit logger the outermost layer — meaning
# rejected (401) requests still get logged.
app.add_middleware(JWTAuthMiddleware)
app.add_middleware(AuditLogMiddleware)


class Health(BaseModel):
    status: str
    service: str
    env: str


@app.get("/health", response_model=Health, tags=["ops"])
def health() -> Health:
    """Liveness only: must not touch dependencies, or a database outage would
    restart otherwise-healthy pods."""
    return Health(status="ok", service="gateway", env=settings.env)


@app.get("/ready", tags=["ops"])
async def ready(response: Response) -> dict[str, Any]:
    """Readiness: verifies the cache database before accepting traffic."""
    try:
        await cache_ping()
    except Exception:
        logger.warning("readiness check failed", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "cache": "unavailable"}
    return {"status": "ok", "cache": "ok"}


@app.get("/secure")
def secure_route(user: User = Depends(clinician_or_admin)) -> dict[str, Any]:
    return {"ok": True, "sub": user.sub, "roles": user.roles}


# --------------------------------------------------------------------------
# FHIR helpers
# --------------------------------------------------------------------------

# Query params that are gateway-internal / meaningless upstream.
_RESERVED_PARAMS = frozenset({"access_token", "token"})


def _clean_params(request: Request) -> dict[str, str]:
    return {
        k: v for k, v in request.query_params.items() if k not in _RESERVED_PARAMS
    }


async def _read(resource: str, resource_id: str) -> dict[str, Any]:
    try:
        return await fhir.read(resource, resource_id)
    except FHIRError as exc:
        # Preserve upstream 404s instead of masking everything as a 502.
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{resource}/{resource_id} not found",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


async def _search(resource: str, params: dict[str, str]) -> dict[str, Any]:
    ttl = CACHE_TTL.get(resource, 300)
    try:
        cached = await get_cached(resource, params, max_age_seconds=ttl)
        if cached is not None:
            return cached
    except Exception:
        # A cache outage must not take the read path down with it.
        cached = None

    try:
        resp = await fhir.search(resource, params)
    except FHIRError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    try:
        await set_cached(resource, params, resp)
    except Exception:
        pass
    return resp


# --------------------------------------------------------------------------
# FHIR routes
#
# IMPORTANT: the literal "/search" routes must be declared *before* the
# "/{id}" routes. FastAPI matches in declaration order, so registering
# "/fhir/patient/{patient_id}" first would swallow "/fhir/patient/search"
# and treat "search" as a patient id.
# --------------------------------------------------------------------------


@app.get("/fhir/patient/search")
async def search_patients(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("Patient", _clean_params(request))


@app.get("/fhir/patient/{patient_id}")
async def get_patient(
    patient_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Patient", patient_id)


# Kept for backwards compatibility with earlier docs/clients.
@app.get("/fhir/patient/{patient_id}/protected")
async def get_patient_protected(
    patient_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Patient", patient_id)


@app.get("/fhir/encounter/search")
async def search_encounters(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("Encounter", _clean_params(request))


@app.get("/fhir/encounter/{encounter_id}")
async def get_encounter(
    encounter_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Encounter", encounter_id)


@app.get("/fhir/observation/search")
async def search_observations(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("Observation", _clean_params(request))


@app.get("/fhir/observation/{obs_id}")
async def get_observation(
    obs_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Observation", obs_id)


@app.get("/fhir/medicationrequest/search")
async def search_medication_requests(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("MedicationRequest", _clean_params(request))


@app.get("/fhir/medicationrequest/{med_id}")
async def get_medication_request(
    med_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("MedicationRequest", med_id)


# --------------------------------------------------------------------------
# Admin cache invalidation
# --------------------------------------------------------------------------


@app.delete("/cache/{resource}")
async def admin_invalidate_cache(
    resource: str,
    patient: str | None = Query(default=None),
    user: User = Depends(admin_only),
) -> dict[str, Any]:
    # Normalise case so "patient" and "Patient" both work.
    canonical = next(
        (r for r in KNOWN_RESOURCES if r.lower() == resource.lower()), None
    )
    if canonical is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown resource '{resource}'. "
            f"Expected one of: {', '.join(sorted(KNOWN_RESOURCES))}",
        )
    try:
        deleted = await invalidate_cache(canonical, patient)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    return {
        "status": "ok",
        "resource": canonical,
        "patient": patient,
        "deleted": deleted,
    }
