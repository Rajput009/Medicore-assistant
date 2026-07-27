#!/usr/bin/env bash
# Fail CI on known vulnerabilities in installed Python packages.
set -euo pipefail
cd "$(dirname "$0")/.."
python -m pip install -q pip-audit
# Ignore only with an explicit documented exception list if needed.
exec python -m pip-audit --progress-spinner=off
