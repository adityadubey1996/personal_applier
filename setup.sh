#!/usr/bin/env bash
# Bootstrap v5 as a self-contained workspace (Python venv + frontend deps).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "==> Steel Browser (Docker)"
if command -v docker >/dev/null 2>&1; then
  docker compose up -d steel-api
  STEEL_API_PORT="${STEEL_API_PORT:-3000}"
  until curl -sf "http://127.0.0.1:${STEEL_API_PORT}/v1/health" >/dev/null 2>&1; do
    sleep 2
  done
  echo "    Steel ready on port ${STEEL_API_PORT}"
else
  echo "    Docker not found — start Steel manually: docker compose up -d steel-api"
fi

echo "==> Python venv (backend/.venv)"
if [[ ! -d backend/.venv ]]; then
  python3 -m venv backend/.venv
fi
# shellcheck disable=SC1091
source backend/.venv/bin/activate
pip install -U pip
pip install -r backend/requirements.txt

if [[ ! -f .env ]] && [[ -f .env.example ]]; then
  cp .env.example .env
  echo "    Created .env from .env.example — add your API keys"
elif [[ -f backend/.env ]] && [[ ! -f .env ]]; then
  cp backend/.env .env
  echo "    Copied backend/.env -> .env"
fi

if [[ ! -f data/resume.pdf ]]; then
  echo "    WARNING: data/resume.pdf missing — add your resume PDF before applying"
fi

echo "==> Frontend (npm)"
if command -v npm >/dev/null 2>&1; then
  (cd frontend && npm install)
else
  echo "    npm not found — skip frontend install"
fi

echo ""
echo "Done. Run:"
echo "  cd backend && source .venv/bin/activate && uvicorn app:app --reload --host 127.0.0.1 --port 8011"
echo "  cd frontend && npm run dev    # http://localhost:5174"
