# V5 LinkedIn Applier (standalone)

Self-contained stack under this folder — no dependency on the parent repo's `backend/`, `sdk/`, or root `docker-compose.yml`.

## Layout

```
v5/
├── docker-compose.yml   # Steel Browser only (port 3000)
├── sdk/steel_agent/     # vendored Steel session SDK
├── backend/             # FastAPI + apply harness + LangGraph orchestrator
├── frontend/            # React UI (Vite dev on 5174)
├── data/                # resume.pdf + anomaly logs (mounted into Steel)
└── setup.sh             # one-shot bootstrap
```

## Quick start

```bash
cd v5
chmod +x setup.sh
./setup.sh
```

Edit `.env` with your LLM API key (`GROQ_API_KEY` or `GOOGLE_API_KEY`).

**Terminal 1 — backend**

```bash
cd v5/backend
source .venv/bin/activate
uvicorn app:app --reload --host 127.0.0.1 --port 8011
```

**Terminal 2 — frontend (dev)**

```bash
cd v5/frontend
npm run dev
```

Open **http://localhost:5174** (API proxied to 8011).

Or build static UI and serve from the backend:

```bash
cd v5/frontend && npm run build
# then open http://127.0.0.1:8011/
```

## Steel Browser

```bash
cd v5
docker compose up -d steel-api
curl -sf http://127.0.0.1:3000/v1/health
```

Put your resume at **`v5/data/resume.pdf`**.

## Environment

Copy `.env.example` → `.env` at the **v5 root**. Optional overrides in `backend/.env`.

| Variable | Purpose |
|----------|---------|
| `GROQ_API_KEY` / `GOOGLE_API_KEY` | LLM for browser-use agent |
| `STEEL_BASE_URL` | Steel HTTP API (default `http://127.0.0.1:3000`) |
| `DATA_DIR` | Resume + logs (default `v5/data`) |
| `APPLY_ENGINE` | `legacy` or `graph` (LangGraph inner loop) |

## VS Code

Open the **`v5`** folder as workspace root and use **Run and Debug** → configs in `.vscode/launch.json`.

## Tests

```bash
cd v5/backend
source .venv/bin/activate
python -m pytest tests/ -q
```
