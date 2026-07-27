import atexit
import logging
import os
import sys
import warnings
from pathlib import Path

import pytest

# Make "backend.*" importable when running pytest from the repo root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ENV", "test")
os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("JWT_SECRET", "test-secret")
# Unit tests must not depend on a live Redis; the in-process fallback covers
# rate-limit and revocation semantics. Integration against real Redis is
# optional and gated behind REDIS_ENABLED=true.
os.environ.setdefault("REDIS_ENABLED", "false")

# pgserver registers an atexit handler that talks to logging after pytest has
# already torn the logging handlers down, which floods the suite summary with
# "--- Logging error ---" noise. Raise the relevant loggers and swallow the
# known late-shutdown warning so CI output stays readable.
logging.getLogger("pgserver").setLevel(logging.CRITICAL)
logging.getLogger("postgres_server").setLevel(logging.CRITICAL)


def _silence_pgserver_atexit_logging() -> None:
    """Last-ditch filter for logging I/O errors during interpreter shutdown."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        # Replace emit with a no-op so a closed stream cannot traceback.
        handler.emit = lambda *args, **kwargs: None  # type: ignore[method-assign]


atexit.register(_silence_pgserver_atexit_logging)

warnings.filterwarnings(
    "ignore",
    message=".*Event loop is closed.*",
    category=RuntimeWarning,
)


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    """Give every test a fresh rate-limit budget.

    Each service's ``app`` is a module-level singleton, so its
    ``RateLimitMiddleware`` instance — and its in-process hit counters — are
    shared by every test in the session. The default budget is 120 requests
    per 60s per subject, and tests overwhelmingly authenticate as the same
    handful of subjects, so a large enough suite eventually starts returning
    429 to tests that have nothing to do with rate limiting.

    That failure mode is order-dependent and looks like a bug in whichever
    test happens to cross the threshold, so the counters are cleared between
    tests. Tests that assert rate-limiting behaviour build their own app with
    an explicit low limit and are unaffected.
    """
    yield

    from backend.common.hardening import RateLimitMiddleware

    seen: set[int] = set()
    for module_name in list(sys.modules):
        if not module_name.startswith("backend.services."):
            continue
        app = getattr(sys.modules[module_name], "app", None)
        # Starlette instantiates middleware lazily, so walk the *built* stack
        # to reach the live limiter holding the counters.
        node = getattr(app, "middleware_stack", None)
        while node is not None and id(node) not in seen:
            seen.add(id(node))
            if isinstance(node, RateLimitMiddleware):
                node._hits.clear()
            node = getattr(node, "app", None)
