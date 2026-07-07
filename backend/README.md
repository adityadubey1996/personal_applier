# Backend V5 (Phase 0)

Minimal backend to validate the V5 flow:

- Create/release a Steel browser session
- Run one browser-use instruction at a time
- Stream logs/audit events to the UI
- Provide iframe URL for live browser visibility

## Run

```bash
cd v5/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8011
```

Then open `http://127.0.0.1:8011/`.

## Environment

Copy `.env.example` to `.env` and set at least:

- `GROQ_API_KEY` (or switch provider to Google and set `GOOGLE_API_KEY`)
- Steel host URLs if different from local defaults
