#!/usr/bin/env bash
# Nest5 ledger — local host on Mac/Linux. Run from this folder after copying from Project Context.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

REQUIRED=(
  requirements.txt
  alembic.ini
  alembic/env.py
  app/main.py
  scripts/seed.py
)
MISSING=()
for f in "${REQUIRED[@]}"; do
  [[ -e "$f" ]] || MISSING+=("$f")
done
if ((${#MISSING[@]} > 0)); then
  echo "Incomplete install in: $ROOT"
  echo "Missing:"
  printf '  - %s\n' "${MISSING[@]}"
  echo ""
  echo "Fix: re-download docs/nest5-ledger.zip from Project Context,"
  echo "delete this folder, unzip again, then run ./run-local.sh from nest5-ledger/"
  exit 1
fi

if ! command -v python3 >/dev/null; then
  echo "Install Python 3.10+ first (python3 --version)"
  exit 1
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
python -m pip install -q --force-reinstall "pydantic==2.10.6" "pydantic-core==2.27.2" "pydantic-settings==2.7.1"
python -c "import pydantic; import pydantic._internal; print('pydantic OK', pydantic.__version__)"
# Bare `alembic` can resolve to the system Python on macOS; always go through the venv python.
if ! python -m alembic upgrade head; then
  echo "Existing database without migration history; stamping it and retrying."
  python -m alembic stamp head
  python -m alembic upgrade head
fi
python scripts/seed.py

echo ""
echo "Starting Nest5 ledger at http://127.0.0.1:8000"
echo "Admin login: username admin, password = your current admin password"
echo "  (first run after the v2 update: nest5admin, or ADMIN_PASSWORD from .env)."
echo "Owner temporary passwords: instance/initial-passwords.txt (first run only)."
echo "Locked out? python scripts/reset_admin.py"
echo "Press Ctrl+C to stop."
echo ""
# Must use venv python — bare `uvicorn` often picks system site-packages on macOS
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
