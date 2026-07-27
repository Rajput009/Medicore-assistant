"""Async FHIR R4 client with OAuth2 client-credentials auth.

The gateway serves requests on an asyncio event loop, so all network I/O here is
async — a blocking client would stall every other in-flight request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from .config import settings


class FHIRError(RuntimeError):
    """Raised when the upstream FHIR server returns an error."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class OAuth2ClientCredentials:
    """Caches a client-credentials access token and refreshes it before expiry."""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        timeout: float = 10.0,
    ):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.timeout = timeout
        self._token: str | None = None
        self._exp: float = 0.0
        self._lock = asyncio.Lock()

    async def _fetch_token(self, client: httpx.AsyncClient) -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.scope:
            data["scope"] = self.scope
        r = await client.post(self.token_url, data=data, timeout=self.timeout)
        r.raise_for_status()
        payload = r.json()

        token = payload.get("access_token")
        if not token:
            raise FHIRError("token endpoint did not return an access_token")

        try:
            expires_in = float(payload.get("expires_in", 300))
        except (TypeError, ValueError):
            expires_in = 300.0
        # Refresh a little early to avoid racing expiry.
        self._exp = time.time() + max(expires_in * 0.9, 1.0)
        self._token = token
        return token

    async def get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._exp:
            return self._token
        # Single-flight: concurrent requests share one token fetch.
        async with self._lock:
            if self._token and time.time() < self._exp:
                return self._token
            return await self._fetch_token(client)

    def invalidate(self) -> None:
        self._token = None
        self._exp = 0.0


class FHIRClient:
    def __init__(
        self,
        base_url: str,
        oauth: OAuth2ClientCredentials,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.oauth = oauth
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        # Reuse one connection pool instead of building a client per request.
        if self._client is None or self._client.is_closed:
            async with self._client_lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        timeout=self.timeout,
                        follow_redirects=False,
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self.oauth.get_token(client)}",
            "Accept": "application/fhir+json",
        }

    async def _request(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        try:
            r = await client.get(url, headers=await self._headers(client), params=params)
            if r.status_code == 401:
                # Token may have been revoked/rotated early: retry once.
                self.oauth.invalidate()
                r = await client.get(
                    url, headers=await self._headers(client), params=params
                )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            raise FHIRError(
                f"FHIR server returned {exc.response.status_code} for {url}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise FHIRError(f"FHIR request failed: {exc}") from exc

    async def read(self, resource: str, resource_id: str) -> dict[str, Any]:
        """Read a single resource by id."""
        if not resource_id:
            raise FHIRError("resource id is required")
        # Escape the id so it cannot break out of the URL path.
        url = f"{self.base_url}/{resource}/{quote(str(resource_id), safe='')}"
        return await self._request(url)

    async def search(
        self,
        resource: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Search a resource type, returning a FHIR Bundle."""
        return await self._request(f"{self.base_url}/{resource}", params=params or {})

    async def get(
        self,
        resource: str,
        id_or_params: str | Mapping[str, Any],
    ) -> dict[str, Any]:
        """Convenience wrapper: read by id when given a string, else search."""
        if isinstance(id_or_params, str):
            return await self.read(resource, id_or_params)
        return await self.search(resource, id_or_params)


_default_client: FHIRClient | None = None


def default_fhir_client() -> FHIRClient:
    """Process-wide FHIR client (lazily constructed, connection-pool reusing)."""
    global _default_client
    if _default_client is None:
        oauth = OAuth2ClientCredentials(
            token_url=settings.fhir_oauth_token_url,
            client_id=settings.fhir_client_id,
            client_secret=settings.fhir_client_secret,
            timeout=settings.fhir_timeout_seconds,
        )
        _default_client = FHIRClient(
            settings.fhir_base_url,
            oauth,
            timeout=settings.fhir_timeout_seconds,
        )
    return _default_client
