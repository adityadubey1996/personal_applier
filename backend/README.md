# Backend V5

See **[../README.md](../README.md)** for the full standalone run guide.

## Run (after `./setup.sh`)

```bash
cd v5/backend
source .venv/bin/activate
uvicorn app:app --reload --host 127.0.0.1 --port 8011
```

SDK lives at `../sdk/steel_agent/` (vendored copy, no pip package).

## Environment

`.env` at **v5 root** (preferred) or `backend/.env` for overrides.
