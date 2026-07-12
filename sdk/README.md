# steel_agent (vendored)

Local copy of `steel_agent` from the parent monorepo `sdk/steel_agent/`.
Used by `v5/backend/app.py` via `sys.path` — no pip install required.

To refresh from upstream:

```bash
rsync -a --delete ../../sdk/steel_agent/ ./steel_agent/
rm -rf ./steel_agent/__pycache__
```
