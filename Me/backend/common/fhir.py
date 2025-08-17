# Minimal FHIR R4 helpers (stubs)
from typing import Dict, Any

def patient_resource_stub(patient_id: str) -> Dict[str, Any]:
    return {
        "resourceType": "Patient",
        "id": patient_id,
        "active": True
    }
