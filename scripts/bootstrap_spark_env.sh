#!/usr/bin/env bash
set -euo pipefail

VENV_DIR="${1:-$HOME/bpm-autosweep-venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" -m venv "$VENV_DIR"
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip wheel
python -m pip install numpy

if python - <<'PY'
import importlib.util
raise SystemExit(0 if importlib.util.find_spec("cupy") else 1)
PY
then
  echo "CuPy is already importable in this environment."
else
  echo "CuPy is not installed in the venv. Install the CUDA-matched cupy wheel for this Spark host if needed."
fi

cat <<EOF

Spark autosweep environment ready:
  source "$VENV_DIR/bin/activate"

The autosweep scripts intentionally avoid pandas, pyarrow, scipy, and zarr.
EOF
