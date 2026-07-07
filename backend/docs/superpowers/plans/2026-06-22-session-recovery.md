# Session Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an in-app "Restart session" that tears down the wedged/orphaned Steel sessions, creates one fresh session, and lands it on the LinkedIn login page — plus fix the session-creation race, the broken `stop_session`, and the login-check log spam.

**Architecture:** All backend work is in `v5/backend/app.py` (FastAPI). A new `POST /api/session/restart` orchestrates teardown → fresh create → navigate-to-login, built from small helpers (`_release_session_by_id`, `_release_all_sessions`, `_navigate`). An `asyncio.Lock` serializes session creation. The frontend adds one "Restart session" button in `LoginSection`.

**Tech Stack:** Python 3.13 / FastAPI / httpx / browser-use (CDP navigation) / Steel SDK (`SteelSession`); React + Vite frontend.

## Global Constraints

- **Restart = always fresh.** No re-attach to a logged-in session; every restart creates a new session and navigates to the login page.
- **Orphan cleanup is aggressive** — restart best-effort releases ALL live Steel sessions (single-user local tool).
- **Recovery is manual** — one button; no background health probe.
- **Login URL** hardcoded: `https://www.linkedin.com/login`.
- **No unit tests** for this work (per decision — it's I/O glue; verify live by clicking Restart). After each backend task, sanity-check with `cd v5/backend && python3 -c "import app"` (must print nothing / exit 0).
- **Frontend build** needs Node 20+: `nvm use 22 && npm run build` (default shell node is 18, which fails).
- **Secrets:** never log or commit API keys; `.env` is git-ignored. (Git is already set up on the `session-recovery` branch — commit when you're ready to; this plan does not prescribe per-task commits.)

---

## File Structure

- **Modify:** `v5/backend/app.py` — add `_session_lock`, lock `_ensure_session`, add `_navigate` / `_release_session_by_id` / `_release_all_sessions` / `LINKEDIN_LOGIN_URL`, add `POST /api/session/restart`, fix `stop_session`, dedupe the login log.
- **Modify:** `v5/frontend/src/components/LoginSection.tsx` — add an `onRestart` prop + "Restart" button.
- **Modify:** `v5/frontend/src/App.tsx` — add `restartSession` handler, pass `onRestart` to `LoginSection`.

---

### Task 1: Serialize session creation with a lock

**Files:**
- Modify: `v5/backend/app.py` (`_ensure_session`, add module-level `_session_lock`)

**Interfaces:**
- Produces: `_ensure_session() -> SteelSession` (unchanged signature; now lock-guarded). `_session_lock: asyncio.Lock`.

- [ ] **Step 1: Add the lock**

In `app.py`, right after `state = RuntimeState()`:

```python
state = RuntimeState()

# Serializes Steel-session creation so concurrent /api/login-status + /api/status
# on page load can't each create one (the "Steel session started ×2" race).
_session_lock = asyncio.Lock()
```

- [ ] **Step 2: Guard `_ensure_session` with a double-checked lock**

Replace the body of `_ensure_session`:

```python
async def _ensure_session() -> SteelSession:
    if state.steel_session and state.session_id:
        return state.steel_session
    async with _session_lock:
        # re-check inside the lock — a concurrent caller may have just created it
        if state.steel_session and state.session_id:
            return state.steel_session
        steel_cfg = SteelConfig(
            steel_http_url=(os.getenv("STEEL_BASE_URL") or "http://127.0.0.1:3000").strip(),
            steel_ws_url=(os.getenv("STEEL_WS_URL") or "ws://127.0.0.1:3000").strip(),
            release_on_exit=False,
        )
        session = await SteelSession.create(steel_cfg)
        state.steel_session = session
        state.session_id = session.session_id
        state.cdp_url = session.cdp_url
        state.viewer_url = session.viewer_url
        state.push_log("info", "Steel session started", session_id=session.session_id)
        return session
```

- [ ] **Step 3: Sanity check** — `cd v5/backend && python3 -c "import app"` (exit 0).

---

### Task 2: Navigation + session-release helpers

**Files:**
- Modify: `v5/backend/app.py` (add `LINKEDIN_LOGIN_URL`, `_navigate`, `_release_session_by_id`, `_release_all_sessions`)

**Interfaces:**
- Produces:
  - `LINKEDIN_LOGIN_URL: str = "https://www.linkedin.com/login"`
  - `async _navigate(url: str) -> None` — best-effort CDP navigation of the live session; no-op if `state.cdp_url` is falsy.
  - `async _release_session_by_id(sid: str) -> None` — best-effort `DELETE /v1/sessions/{sid}`.
  - `async _release_all_sessions() -> int` — release every live Steel session; returns count attempted.

- [ ] **Step 1: Add the login-URL constant**

In `app.py`, near the other module constants (e.g. after `LEDGER_PATH`):

```python
LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
```

- [ ] **Step 2: Add the helpers**

Next to `_attach_logged_in_session` / `_check_login`:

```python
async def _navigate(url: str) -> None:
    """Best-effort: drive the live session's browser to `url` (e.g. the login page).
    A failure is logged but never raised — the viewer still shows the session."""
    if not state.cdp_url:
        return
    try:
        from browser_use import Browser
        b = Browser(cdp_url=state.cdp_url)
        await b.start()
        try:
            await b.navigate_to(url)
        finally:
            with contextlib.suppress(Exception):
                await b.stop()
    except Exception as e:  # noqa: BLE001
        state.push_log("warn", f"navigate to {url} failed: {e}")


async def _release_session_by_id(sid: str) -> None:
    """Best-effort release of a Steel session by id (works for attached sessions
    where we hold no SteelSession object)."""
    if not sid:
        return
    base = (os.getenv("STEEL_BASE_URL") or "http://127.0.0.1:3000").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.delete(f"{base}/v1/sessions/{sid}")
    except Exception as e:  # noqa: BLE001
        state.push_log("warn", f"release session {sid[:8]} failed: {e}")


async def _release_all_sessions() -> int:
    """Best-effort release of ALL live Steel sessions (orphan cleanup on restart).
    Returns the number of sessions we attempted to release."""
    base = (os.getenv("STEEL_BASE_URL") or "http://127.0.0.1:3000").rstrip("/")
    attempted = 0
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/v1/sessions")
            live = [s for s in r.json().get("sessions", []) if s.get("status") != "released"]
        for s in live:
            attempted += 1
            await _release_session_by_id(s["id"])
    except Exception as e:  # noqa: BLE001
        state.push_log("warn", f"release-all sessions failed: {e}")
    return attempted
```

- [ ] **Step 3: Sanity check** — `cd v5/backend && python3 -c "import app"` (exit 0).

---

### Task 3: `POST /api/session/restart` endpoint

**Files:**
- Modify: `v5/backend/app.py` (add the endpoint after `start_session`)

**Interfaces:**
- Consumes: `_ensure_session`, `_navigate`, `_release_all_sessions`, `_interactive_viewer_url`, `LINKEDIN_LOGIN_URL`, `state`.
- Produces: `POST /api/session/restart -> {"session_id", "interactive_viewer_url", "cdp_url"}` (503 if Steel can't create).

- [ ] **Step 1: Add the endpoint**

In `app.py`, after the `start_session` handler:

```python
@app.post("/api/session/restart")
async def restart_session() -> dict[str, Any]:
    """Recovery: tear down the current + orphaned Steel sessions, create a fresh
    one, and navigate it to the LinkedIn login page. Always fresh (no re-attach)."""
    # 1. stop any running work
    if state.command_task and not state.command_task.done():
        state.command_task.cancel()
        with contextlib.suppress(Exception):
            await state.command_task
    if state.batch_task and not state.batch_task.done():
        state.batch_stop_event.set()
        state.batch_task.cancel()
        with contextlib.suppress(Exception):
            await state.batch_task

    # 2. tear down current + orphaned sessions
    if state.steel_session:
        with contextlib.suppress(Exception):
            await state.steel_session.release()
    await _release_all_sessions()

    # 3. reset all session-scoped state
    state.steel_session = None
    state.session_id = None
    state.cdp_url = None
    state.viewer_url = None
    state.login_checked = None
    state.last_result = None
    state.apply_hstate = None
    state.running = False
    state.batch_running = False

    # 4. fresh session
    try:
        await _ensure_session()
    except Exception as e:  # noqa: BLE001
        state.push_log("error", f"restart: could not create session: {e}")
        raise HTTPException(status_code=503, detail=f"Could not create a new Steel session: {e}")

    # 5. land on the login page so the viewer is immediately usable
    await _navigate(LINKEDIN_LOGIN_URL)
    state.push_log("info", "Session restarted (fresh) — navigated to LinkedIn login")

    # 6. fresh connection info for the UI
    return {
        "session_id": state.session_id,
        "interactive_viewer_url": _interactive_viewer_url(),
        "cdp_url": state.cdp_url,
    }
```

- [ ] **Step 2: Sanity check** — `cd v5/backend && python3 -c "import app"` (exit 0).

---

### Task 4: Fix `stop_session` for attached sessions

**Files:**
- Modify: `v5/backend/app.py` (`stop_session`)

**Interfaces:**
- Consumes: `_release_session_by_id`.
- Produces: `DELETE /api/session` now releases the session even when it was *attached* (no `steel_session` object).

- [ ] **Step 1: Fix `stop_session`**

Replace the release block in `stop_session`:

```python
@app.delete("/api/session")
async def stop_session() -> dict[str, bool]:
    if state.running and state.command_task:
        state.command_task.cancel()
        with contextlib.suppress(Exception):
            await state.command_task
    if state.steel_session:
        with contextlib.suppress(Exception):
            await state.steel_session.release()
    elif state.session_id:
        # attached session — no SteelSession object, release by id
        await _release_session_by_id(state.session_id)
    state.steel_session = None
    state.session_id = None
    state.cdp_url = None
    state.viewer_url = None
    state.login_checked = None
    state.push_log("info", "Steel session released")
    return {"released": True}
```

- [ ] **Step 2: Sanity check** — `cd v5/backend && python3 -c "import app"` (exit 0).

---

### Task 5: Dedupe the login-check log

**Files:**
- Modify: `v5/backend/app.py` (`login_status`)

**Interfaces:**
- Produces: `login_status` logs the login state only when it changes.

- [ ] **Step 1: Dedupe the log**

In `login_status`, replace the logging block:

```python
    logged_in = await _check_login(platform)
    if logged_in != state.login_checked:  # log only on transition, not every poll
        if logged_in:
            state.push_log("info", f"Login check: logged_in=True ({platform})")
        else:
            state.push_log("info", f"Login check: NOT logged in to {platform} — viewer shown for manual login")
    state.login_checked = logged_in
```

- [ ] **Step 2: Sanity check** — `cd v5/backend && python3 -c "import app"` (exit 0).

---

### Task 6: Frontend "Restart" button

**Files:**
- Modify: `v5/frontend/src/components/LoginSection.tsx` (add `onRestart` prop + button)
- Modify: `v5/frontend/src/App.tsx` (add `restartSession` handler, pass `onRestart`)

**Interfaces:**
- Consumes: `POST /api/session/restart` (Task 3) → `{ interactive_viewer_url }`.

- [ ] **Step 1: Add the handler in `App.tsx`**

Near the other handlers (after `checkLogin`), using the existing `api` helper:

```tsx
  const restartSession = useCallback(async () => {
    try {
      const data = await api<{ interactive_viewer_url: string }>("/api/session/restart", "POST")
      if (data.interactive_viewer_url) setViewerUrl(data.interactive_viewer_url)
      await checkLogin()
    } catch (e) { console.error("Restart failed:", e) }
  }, [checkLogin])
```

- [ ] **Step 2: Pass `onRestart` to `LoginSection`**

Update the render (around line 125) — add `onRestart={restartSession}` to the existing props:

```tsx
          <LoginSection loggedIn={loggedIn} checking={loginChecking} interactiveUrl={interactiveUrl}
            onCheck={checkLogin} onImLoggedIn={() => setLoggedIn(true)} setViewerUrl={setViewerUrl}
            onRestart={restartSession} />
```

- [ ] **Step 3: Add the prop + button in `LoginSection.tsx`**

Add `onRestart` to `Props` and the destructure:

```tsx
interface Props {
  loggedIn: boolean
  checking: boolean
  interactiveUrl: string
  onCheck: () => void
  onImLoggedIn: () => void
  setViewerUrl: (url: string) => void
  onRestart: () => void
}

export function LoginSection({ loggedIn, checking, interactiveUrl, onCheck, onImLoggedIn, setViewerUrl, onRestart }: Props) {
```

Add the button to the button row (after the "Check" button so it's always visible):

```tsx
        <button onClick={onRestart} disabled={checking} className="flex-1 px-2.5 py-1.5"
          style={{ background: "var(--red-dim)", color: "var(--red)", border: "1px solid rgba(239,68,68,0.3)" }}>
          <RefreshCw size={12} /> Restart
        </button>
```

- [ ] **Step 4: Build the frontend (Node 20+)**

```bash
cd v5/frontend && nvm use 22 && npm run build
```
Expected: `✓ built in …`, no TypeScript errors, `dist/` updated.

---

## Final verification (live smoke check)

- [ ] `cd v5/backend && python3 -c "import app"` → exit 0 (no syntax/import errors).
- [ ] Start backend: `cd v5/backend && uvicorn app:app --reload --port 8001`; open the UI.
- [ ] Click **Restart**. Expected: a fresh session is created, the log shows `Session restarted (fresh) — navigated to LinkedIn login`, and the viewer iframe loads the LinkedIn **login page** (not a blank browser).
- [ ] Log in in the viewer → **Check** → shows "Connected".
