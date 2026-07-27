import threading
import time
from typing import Any

import httpx


class JWKSFetcher:
    """Fetches and caches a JWKS document.

    ``get_keys`` returns the ``keys`` list. The cache is only populated on a
    successful fetch, so a transient IdP outage never poisons the cache.
    """

    def __init__(self, url: str, ttl: int = 600, timeout: float = 10.0):
        self.url = url
        self.ttl = ttl
        self.timeout = timeout
        self._keys: list[dict[str, Any]] | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def _fetch(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(self.url)
            r.raise_for_status()
            data = r.json()
        keys = data.get("keys")
        if not isinstance(keys, list):
            raise ValueError(f"JWKS document at {self.url} has no 'keys' array")
        return keys

    def get_keys(self) -> list[dict[str, Any]]:
        now = time.time()
        if self._keys is not None and now < self._expires:
            return self._keys
        with self._lock:
            # Re-check inside the lock: another thread may have refreshed.
            now = time.time()
            if self._keys is not None and now < self._expires:
                return self._keys
            try:
                keys = self._fetch()
            except Exception:
                # Serve stale keys rather than failing every request outright.
                if self._keys is not None:
                    return self._keys
                raise
            self._keys = keys
            self._expires = time.time() + self.ttl
            return self._keys

    def refresh(self) -> list[dict[str, Any]]:
        """Force a re-fetch, e.g. after an unknown ``kid`` is seen."""
        with self._lock:
            keys = self._fetch()
            self._keys = keys
            self._expires = time.time() + self.ttl
            return self._keys
