import os
import sys
from pathlib import Path

# Make "backend.*" importable when running pytest from the repo root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ENV", "test")
os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("JWT_SECRET", "test-secret")
