# Session Recovery — Steel Session Restart + Login Navigation

**Date:** 2026-06-22
**Status:** Design approved (always-fresh restart); pre-implementation
**Component:** `v5/backend` (`app.py`) + `v5/frontend` (`LoginSection`)
**Sequence:** Sub-project 1 of "Recovery → Filters → Outreach (framework emerges)"

---

## 1. Problem

The Steel browser session wedges and there is **no in-app recovery** — the only
recourse is killing the backend, which makes the problem worse. Observed
symptoms and their root causes:

1. **Session-creation race + orphan pileup.** `_ensure_session` (`app.py:251`)
   has no lock. On page load the frontend fires `/api/login-status` and
   `/api/status` concurrently; each sees `state.session_id is None` and creates a
   Steel session → the **"Steel session started" ×2** in the logs. With
   `release_on_exit=False` and `uvicorn --reload`, old sessions are never
   released, so they **accumulate** on the Steel server. A loaded/orphaned Steel
   server makes the live session slow or unresponsive.

2. **The viewer never reaches a login page.** A fresh Steel session opens on
   `about:blank`; nothing navigates it to LinkedIn. `_check_login` (`app.py:290`)
   only inspects cookies — it never drives the browser. So the interactive viewer
   shows a **blank browser with no login form**; "view opener" looks dead.

3. **No recovery path; `stop_session` is half-broken.** `DELETE /api/session`
   (`app.py:591`) only releases `state.steel_session`. When a session was
   *attached* (`_attach_logged_in_session` sets `session_id`/`cdp_url` but **not**
   `steel_session`), release is a no-op and state is left half-cleared. There is
   no restart endpoint and no UI button.

4. **Login-check log spam.** `/api/login-status` logs "NOT logged in" on **every**
   call (`app.py:568`); while logged out the UI keeps polling → the flood seen in
   the terminal.

The traceback the user saw (`CancelledError` in `sse.py` on shutdown) is benign —
`--reload` cancelling the log-stream generator, not a cause.

## 2. Goals / Non-goals

**Goals**
- One in-app action to recover from a wedged session: tear down and create a fresh
  Steel session with a fresh CDP/viewer connection.
- The fresh session lands on the **LinkedIn login page** so the user can log in
  immediately in the viewer.
- Stop creating duplicate/orphaned sessions (race + accumulation).
- Make `stop_session` correctly release **any** session (created or attached).
- Quiet the login-check log spam.

**Non-goals (deferred)**
- Auto-detection / health-polling of a wedged session (manual button only).
- Preserving an existing login across restart (decision: **always create fresh**).
- Reconnect-without-new-session ("websocket reconnect" to the same session) —
  superseded by always-fresh.
- Multi-platform login navigation (LinkedIn URL only; trivially generalizable later).

## 3. Decisions (settled in brainstorming)

- **Restart = always fresh.** No attempt to re-attach to an existing logged-in
  session. Predictable: every restart yields a brand-new session navigated to the
  login page; the user re-logs in.
- **Orphan cleanup is aggressive.** On restart, best-effort release **all** live
  Steel sessions (single-user local tool), then create one fresh. Acceptable
  because always-fresh already discards any existing login.
- **Recovery is manual.** A single "Restart session" button; no background probe.

## 4. Design

### 4.1 New endpoint — `POST /api/session/restart`

Sequence:
1. **Stop work.** If a command task is running, cancel + await it. If a batch task
   is running, set `batch_stop_event`, cancel + await. Existing `finally` blocks
   clear `running` / `batch_running`.
2. **Tear down.** Release the current `state.steel_session` if present; then
   best-effort release every live session from `GET {STEEL_BASE_URL}/v1/sessions`
   (`DELETE /v1/sessions/{id}` each, via httpx — same style as
   `_attach_logged_in_session`). Wrapped so Steel-API flakiness never fails the
   restart.
3. **Reset state.** `steel_session`, `session_id`, `cdp_url`, `viewer_url`,
   `login_checked`, `last_result`, `apply_hstate` → cleared.
4. **Create fresh** via `_ensure_session()` (now lock-guarded — §4.2).
5. **Navigate to login** via `_navigate("https://www.linkedin.com/login")` (§4.3)
   so the viewer opens on the login form.
6. **Return** `{ session_id, interactive_viewer_url, cdp_url }`.

Returns 503 if `_ensure_session` cannot create (Steel down), with a clear message.

### 4.2 Session-creation lock

Module-level `_session_lock = asyncio.Lock()`. `_ensure_session` acquires it and
**re-checks** `state.steel_session and state.session_id` inside the lock before
creating — so two concurrent callers can never create two sessions. The existing
early-return for an already-live session stays as a cheap pre-lock fast path.

### 4.3 `_navigate(url)` helper

Drive the live session's browser to a URL using the existing browser-use pattern
(identical to `LinkedInAdapter.discover_jobs`):

```python
async def _navigate(url: str) -> None:
    if not state.cdp_url:
        return
    from browser_use import Browser
    b = Browser(cdp_url=state.cdp_url)
    await b.start()
    try:
        await b.navigate_to(url)
    finally:
        with contextlib.suppress(Exception):
            await b.stop()
```

Best-effort: a navigation failure logs a warning but does **not** fail the restart
(the viewer still shows the blank session; the user can type the URL).

### 4.4 Fix `stop_session`

Release by `session_id` even when `state.steel_session is None` (the attached
case): `DELETE /v1/sessions/{session_id}` via httpx. Keep the existing
`steel_session.release()` path for created sessions. Then clear state as today.

### 4.5 Quiet the login-check log

In `login_status`, emit the "logged_in" / "NOT logged in" line **only when
`logged_in != state.login_checked`** (a transition), not on every poll. Set
`state.login_checked` afterward as today.

### 4.6 Frontend — "Restart session" button

In `LoginSection`: a **"Restart session"** button → `POST /api/session/restart`.
On success, set the viewer iframe `src` to the returned `interactive_viewer_url`
(forces a fresh connection) and re-run `checkLogin`. Disable while a run is
active. Rebuild with Node 22 (`nvm use 22 && npm run build`; vite needs 20+).

## 5. Error handling

- Restart never throws on Steel-API errors during teardown/orphan-cleanup
  (best-effort, logged).
- `_navigate` failure → warn + continue (session still created).
- `_ensure_session` failure (Steel down) → restart returns 503 with a clear
  message the UI surfaces; the lock leaves state clean (no half-set session).

## 6. Testing

`tests/test_session_recovery.py` — no live Steel (mock the Steel calls). Follows
the repo convention (plain `asyncio.run` + `assert`, `if __name__ == "__main__"`,
no pytest), matching `test_heal.py` / `test_wiring.py`:
- **Lock prevents double-create:** patch `SteelSession.create` with a slow stub;
  fire two concurrent `_ensure_session()`; assert `create` called **once** and one
  session in state.
- **Restart resets state + creates fresh:** mock list/release/create and
  `_navigate`; call the restart handler; assert old session released, state reset,
  new session set, `_navigate` called with the LinkedIn login URL.
- **`stop_session` releases an attached session:** set `session_id` with
  `steel_session=None`; assert a DELETE is issued for that id and state is cleared.
- **Log dedupe:** two `login_status` calls with unchanged `logged_in` → one log
  line for that state.

## 7. Risks / assumptions

- **Aggressive orphan cleanup** assumes single-user local Steel (no other
  consumers). Documented; revisit if Steel becomes shared.
- **`navigate_to` on a fresh session** assumes the Steel browser accepts CDP right
  after create. The SDK health-checks on create, so the browser is up; the
  best-effort wrapper degrades gracefully if navigation races readiness.
- **Always-fresh loses login each restart** — accepted per the decision; the
  login-page navigation makes re-login one step.

## 8. Out of scope (future)

- Auto health-detection + "session looks unresponsive → reconnect" hint.
- Reconnect-to-same-session (keep login) — only if always-fresh proves annoying.
- Platform-parameterized login URL (when a 2nd platform arrives).
