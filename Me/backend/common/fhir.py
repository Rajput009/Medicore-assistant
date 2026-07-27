# Minimal FHIR R4 helpers (stubs)
from typing import Any


def patient_resource_stub(patient_id: str) -> dict[str, Any]:
    return {
        "resourceType": "Patient",
        "id": patient_id,
        "active": True
    }
