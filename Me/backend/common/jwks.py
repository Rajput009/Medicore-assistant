import time, threading, httpx
from typing import Dict, Any, Optional

class JWKSFetcher:
    def __init__(self, url: str, ttl: int = 600):
        self.url = url
        self.ttl = ttl
        self._keys: Optional[Dict[str, Any]] = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def _fetch(self) -> Dict[str, Any]:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(self.url)
            r.raise_for_status()
            return r.json()

    def get_keys(self) -> Dict[str, Any]:
        now = time.time()
        if self._keys and now < self._expires:
            return self._keys
        with self._lock:
            if self._keys and now < self._expires:
                return self._keys
            data = self._fetch()
            self._keys = data.get('keys', [])
            self._expires = now + self.ttl
            return self._keys
