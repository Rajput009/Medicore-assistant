import atexit
import logging
import os
import sys
import warnings
from pathlib import Path

# Make "backend.*" importable when running pytest from the repo root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ENV", "test")
os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("JWT_SECRET", "test-secret")

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
