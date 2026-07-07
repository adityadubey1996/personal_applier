from __future__ import annotations

import asyncio
import json
import re

# Job IDs live in href="/jobs/view/<id>"; this is the only thing discovery needs.
_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)")


def _job_ids_from_hrefs(hrefs, n: int) -> list[str]:
    """Pull the first `n` unique numeric job IDs out of a list of hrefs, in order."""
    ids: list[str] = []
    for h in hrefs or []:
        m = _JOB_ID_RE.search(str(h))
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids[:n]


class LinkedInAdapter:
    name = "linkedin"
    typeahead_selector = "[role=option].basic-typeahead__selectable"
    field_hints: dict[str, str] = {}

    async def entry(self, agent) -> None:
        """Click the Easy Apply button to open the application modal."""
        agent.add_new_task("Click the Easy Apply button to open the application modal.")

    async def is_submitted(self, agent) -> bool:
        """Return True if the page shows LinkedIn's application-sent confirmation."""
        try:
            browser_session = agent.browser_session
            cdp_session = await browser_session.get_or_create_cdp_session()
            result = await cdp_session.cdp_client.send.Runtime.evaluate(
                params={
                    "expression": "document.body.innerText",
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                session_id=cdp_session.session_id,
            )
            text: str = ((result.get("result") or {}).get("value") or "")
            return "application was sent" in text.lower()
        except Exception:
            return False

    async def discover_jobs(self, ctx: dict, n: int) -> list[str]:
        """Deterministically scrape Easy-Apply job IDs from the LinkedIn search page.

        This is a mechanical DOM read, NOT an LLM task. (An LLM agent here loops and
        fails: browser-use only persists a short "Found N elements" summary across
        steps, so the model loses the hrefs and never parses the IDs.) The IDs sit in
        href="/jobs/view/<id>" — one navigate + one Runtime.evaluate gets them all.

        ctx keys: cdp_url, keywords, location, start (pagination offset), log.
        """
        from urllib.parse import quote
        from browser_use import Browser  # lazy — browser_use may not be installed

        cdp_url: str = ctx["cdp_url"]
        keywords: str = ctx["keywords"]
        location: str = ctx["location"]
        start: int = ctx["start"]
        log = ctx["log"]
        filters: dict = ctx.get("filters") or {}

        # Append any LinkedIn f_* facet the caller set (empty values are omitted).
        filter_q = "".join(f"&{k}={quote(str(v), safe='')}" for k, v in filters.items() if v)
        easy_apply = "&f_AL=true" if ctx.get("easy_apply_only", True) else ""
        search_url = (
            "https://www.linkedin.com/jobs/search/"
            f"?keywords={quote(keywords, safe='')}&location={quote(location, safe='')}"
            f"{easy_apply}{filter_q}&start={start}"
        )
        log("info", f"[SEARCH] {keywords!r} in {location!r} start={start} (deterministic scrape)")

        expr = (
            "JSON.stringify(Array.from("
            "document.querySelectorAll('a[href*=\"/jobs/view/\"]')).map(function(a){return a.href;}))"
        )
        browser = Browser(cdp_url=cdp_url)
        await browser.start()
        try:
            await browser.navigate_to(search_url)
            ids: list[str] = []
            for _ in range(5):  # listings lazy-load — poll a few times before giving up
                await asyncio.sleep(1.5)
                cdp = await browser.get_or_create_cdp_session()
                res = await cdp.cdp_client.send.Runtime.evaluate(
                    params={"expression": expr, "returnByValue": True, "awaitPromise": True},
                    session_id=cdp.session_id,
                )
                raw = (res.get("result") or {}).get("value") or "[]"
                try:
                    hrefs = json.loads(raw)
                except Exception:
                    hrefs = []
                ids = _job_ids_from_hrefs(hrefs, n)
                if ids:
                    break
            log("info", f"[SEARCH] Found {len(ids)} job IDs")
            return ids
        except Exception as e:  # noqa: BLE001
            log("warn", f"[SEARCH] discovery failed: {e}")
            return []
        finally:
            # Disconnect the browser-use wrapper; the underlying Steel session
            # (release_on_exit=False) survives and the apply step reconnects fresh.
            try:
                await browser.stop()
            except Exception:
                pass
