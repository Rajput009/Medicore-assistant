import os, time
import httpx
from typing import Dict, Any, Optional
from .config import settings

class OAuth2ClientCredentials:
    def __init__(self, token_url: str, client_id: str, client_secret: str):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._exp: float = 0.0

    def _fetch_token(self) -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        with httpx.Client(timeout=10.0) as c:
            r = c.post(self.token_url, data=data)
            r.raise_for_status()
            payload = r.json()
        self._token = payload.get("access_token")
        self._exp = time.time() + float(payload.get("expires_in", 300)) * 0.9
        return self._token

    def get_token(self) -> str:
        if not self._token or time.time() > self._exp:
            return self._fetch_token()
        return self._token

class FHIRClient:
    def __init__(self, base_url: str, oauth: OAuth2ClientCredentials):
        self.base_url = base_url.rstrip("/")
        self.oauth = oauth

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.oauth.get_token()}",
                "Accept": "application/fhir+json"}

    def get(self, resource: str, id_or_params: str | Dict[str, Any]):
        url = f"{self.base_url}/{resource}"
        with httpx.Client(timeout=10.0) as c:
            if isinstance(id_or_params, str):
                r = c.get(f"{url}/{id_or_params}", headers=self._headers())
            else:
                r = c.get(url, headers=self._headers(), params=id_or_params)
            r.raise_for_status()
            return r.json()

def default_fhir_client() -> FHIRClient:
    oauth = OAuth2ClientCredentials(
        token_url=settings.fhir_oauth_token_url,
        client_id=settings.fhir_client_id,
        client_secret=settings.fhir_client_secret
    )
    return FHIRClient(settings.fhir_base_url, oauth)
