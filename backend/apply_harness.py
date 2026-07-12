"""V5 apply harness — profile.yaml-driven resolver + scoped browser-use hands.

Brain side: a Resolver loads the detailed profile.yaml and answers the browser
agent's get_field_value(label) requests PER FIELD — the profile is never inlined
into the agent prompt (low tokens). The real resume.pdf is served as a file the
agent uploads. Every decision is streamed to the log (audit).

HITL: protected/unknown fields pause for human input via asyncio.Event wait
(step_timeout=1800s so the step survives the pause). Non-protected unknown
fields are answered conservatively by the agent.

Submit policy: "hitl" (default) = pause for review; "auto_if_clean" = submit
automatically if every field was resolved from profile; "auto" = always submit.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

V5_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(__file__).resolve().parent
PROFILE_PATH = APP_DIR / "profile.yaml"
_DEFAULT_DATA = V5_ROOT / "data"
RESUME_PATH = str(
    Path(os.getenv("DATA_DIR") or os.getenv("STEEL_BROWSER_DATA_MOUNT") or _DEFAULT_DATA)
    / "resume.pdf"
)

LogFn = Callable[..., None]


def _load_profile() -> dict[str, Any]:
    try:
        return yaml.safe_load(PROFILE_PATH.read_text()) or {}
    except Exception:
        return {}


def _norm(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _num(v: Any) -> str:
    # ponytail: LinkedIn numeric fields reject ₹/commas/units -> emit bare digits.
    m = re.search(r"\d[\d,]*\.?\d*", str(v or ""))
    return m.group(0).replace(",", "") if m else ""


def _plausibly_numeric(v: str) -> bool:
    # A numeric ANSWER leads with the number (optionally an INR/currency prefix).
    # Prose like "see resume page 2" or "N/A" must NOT be coerced to a stray digit
    # and silently entered as a salary — escalate those instead.
    return bool(re.match(r"^\s*(rs\.?|inr|usd|₹|\$|€|£)?\s*\d", v or "", re.I))


def _is_open_question(label: str) -> bool:
    """Open-ended free-text question (cover letter, 'why', 'describe') with no profile
    answer should go to a human, not be fabricated by the agent onto an employer's form."""
    low = (label or "").lower()
    if len(label or "") > 120:
        return True
    return any(k in low for k in (
        "why ", "describe", "cover letter", "tell us", "what makes", "explain",
        "in your own words", "elaborate", "motivat", "passionate",
    ))


# JS for set_field: find the question container by label text, then set the right control
# (radio / checkbox / native select / text) by VALUE — at the DOM level, so it works on
# controls browser-use never indexed (hidden inputs behind styled labels). __LABEL__/__VALUE__
# are replaced with json.dumps() values at call time. On miss it returns probe data.
_SET_FIELD_JS = r"""
(function(label, value){
  function norm(s){return (s||'').trim().toLowerCase().replace(/\s+/g,' ');}
  // option label "starts with the value as a whole word": matches "Yes, I am authorized" for
  // "Yes" but NOT "Not applicable" for "No" (the next char after the value must be a non-letter).
  function startsWord(t, v){
    if (!t || !v) return false;
    if (t.indexOf(v) !== 0) return false;
    return t.length === v.length || /[^a-z0-9]/.test(t.charAt(v.length));
  }
  var nl = norm(label), nv = norm(value);
  var sels = '.fb-dash-form-element, fieldset, [data-test-form-element], .artdeco-form__item';
  var boxes = [].slice.call(document.querySelectorAll(sels));
  var box = null, best = 1e9;
  for (var i=0;i<boxes.length;i++){
    var tx = norm(boxes[i].innerText);
    if (tx.indexOf(nl) !== -1 && boxes[i].innerText.length < best){ box = boxes[i]; best = boxes[i].innerText.length; }
  }
  var scope = box || document;

  // ---- radio / checkbox: EXACT match across all options first; only then a safe word-boundary
  // fallback. Never a raw substring/contains match — that silently clicks the wrong option
  // (e.g. "No" -> "Not applicable"), the exact failure this deterministic setter must avoid.
  var inputs = [].slice.call(scope.querySelectorAll('input[type=radio], input[type=checkbox]'));
  function labOf(el){
    var lab = (el.id && document.querySelector('label[for="'+CSS.escape(el.id)+'"]')) || el.closest('label');
    return {lab: lab, t: norm(lab ? lab.innerText : (el.getAttribute('aria-label') || el.value))};
  }
  function clickRet(el, info){
    el.click();
    return JSON.stringify({ok:true, type:el.type, matched:(info.lab?info.lab.innerText:el.value).slice(0,80)});
  }
  for (var j=0;j<inputs.length;j++){ var a=labOf(inputs[j]); if (a.t === nv) return clickRet(inputs[j], a); }
  for (var j2=0;j2<inputs.length;j2++){ var b=labOf(inputs[j2]); if (startsWord(b.t, nv)) return clickRet(inputs[j2], b); }
  if (inputs.length){
    return JSON.stringify({ok:false, type:'radio', error:'no option matched value',
                           options:inputs.map(function(e){return labOf(e).t;}).slice(0,12)});
  }

  // ---- native select: exact option first, then word-boundary
  var sel = scope.querySelector('select');
  if (sel){
    var opts = [].slice.call(sel.options);
    var pick = null;
    for (var k=0;k<opts.length;k++){ if (norm(opts[k].text) === nv){ pick = opts[k]; break; } }
    if (!pick){ for (var k2=0;k2<opts.length;k2++){ if (startsWord(norm(opts[k2].text), nv)){ pick = opts[k2]; break; } } }
    if (pick){ sel.value = pick.value; sel.dispatchEvent(new Event('change',{bubbles:true}));
               return JSON.stringify({ok:true, type:'select', matched:pick.text}); }
    return JSON.stringify({ok:false, type:'select', error:'no option matched', options:opts.map(function(o){return o.text;}).slice(0,12)});
  }

  // ---- text / number / textarea
  var txt = scope.querySelector('input[type=text], input[type=tel], input[type=number], input:not([type]), textarea');
  if (txt){
    var proto = (txt.tagName === 'TEXTAREA') ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, 'value');
    setter.set.call(txt, value);
    txt.dispatchEvent(new Event('input',{bubbles:true}));
    txt.dispatchEvent(new Event('change',{bubbles:true}));
    return JSON.stringify({ok:true, type:'text'});
  }
  return JSON.stringify({ok:false, error:'no settable control under label',
                         probe:(box ? box.innerText.slice(0,200) : 'no container matched label')});
})(__LABEL__, __VALUE__)
"""


def _save_learned_answer(norm_label: str, value: str) -> None:
    """Write-back a learned answer to profile.yaml learned_answers (best-effort)."""
    try:
        profile = yaml.safe_load(PROFILE_PATH.read_text()) or {}
        la = profile.get("learned_answers") or {}
        la[norm_label] = value
        profile["learned_answers"] = la
        PROFILE_PATH.write_text(yaml.dump(profile, default_flow_style=False, allow_unicode=True))
    except Exception:
        pass


# Protected field keywords — answers are never cached, always escalate to HITL
_PROTECTED_KEYS = [
    "gender", "race", "ethnic", "hispanic", "latino", "veteran", "disab",
    "work author", "sponsor", "visa", "citizen", "immigr",
]


def _is_protected(norm_label: str) -> bool:
    return any(k in norm_label for k in _PROTECTED_KEYS)


class Resolver:
    """Maps an arbitrary field label to a grounded value from profile.yaml, or None."""

    def __init__(self) -> None:
        p = _load_profile()
        b = p.get("basics", {}) or {}
        loc = b.get("location", {}) or {}
        name = b.get("name", "") or ""
        self.first = b.get("first_name") or (name.split()[0] if name else "")
        self.last = b.get("last_name") or (" ".join(name.split()[1:]) if name else "")
        self.name = name
        self.preferred = b.get("preferred_name") or self.first
        self.email = b.get("email", "") or ""
        digits = re.sub(r"\D", "", b.get("phone", "") or "")
        self.phone = digits[-10:] if len(digits) >= 10 else digits
        self.city = loc.get("city", "") or ""
        self.region = loc.get("region", "") or ""
        self.postal = str(loc.get("postalCode", "") or "")
        self.country = loc.get("country", "") or ""
        self.address = loc.get("address_line1", "") or ""
        profs = {(pr.get("network", "") or "").lower(): pr.get("url", "") for pr in (b.get("profiles") or [])}
        self.linkedin = profs.get("linkedin") or b.get("url", "") or ""
        self.github = profs.get("github", "") or ""
        self.portfolio = profs.get("portfolio", "") or ""
        self.skill_years = {str(k).lower(): v for k, v in (p.get("skill_years") or {}).items()}
        self.prefs = p.get("preferences", {}) or {}
        work = p.get("work", []) or []
        self.cur_company = (work[0].get("company", "") if work else "") or ""
        self.cur_title = (work[0].get("position", "") if work else "") or ""
        flat: list[str] = []
        for v in (p.get("skills", {}) or {}).values():
            if isinstance(v, list):
                flat += [str(x) for x in v]
        self.skills_hint = ", ".join(flat[:20])
        # Learned answers from prior HITL sessions
        self.learned: dict[str, str] = {
            str(k): str(v) for k, v in (p.get("learned_answers") or {}).items()
        }

    def resolve(self, label: str) -> Optional[tuple[str, str]]:
        q = _norm(label)
        if not q:
            return None

        def R(v: Any, src: str) -> Optional[tuple[str, str]]:
            return (str(v), src) if v not in (None, "") else None

        # Learned answers are checked first (highest priority after resume)
        if q in self.learned:
            return (self.learned[q], "learned_answers")

        # EEO / demographics — safe default (never HITL these, never cache)
        if any(w in q for w in ["gender", "race", "ethnic", "hispanic", "latino", "veteran", "disab"]):
            return ("Decline to self-identify", "demographics")
        if "country code" in q or ("country" in q and "code" in q):
            return ("India (+91)", "basics.phone")
        if "email" in q:
            return R(self.email, "basics.email")
        if "phone" in q or "mobile" in q or q == "tel":
            return R(self.phone, "basics.phone")
        if "first name" in q or "given name" in q or "given names" in q or "forename" in q:
            return R(self.first, "basics.first_name")
        if "last name" in q or "family name" in q or "surname" in q:
            return R(self.last, "basics.last_name")
        if "preferred name" in q or "nickname" in q:
            return R(self.preferred, "basics.preferred_name")
        if "full name" in q or q == "name" or q.endswith(" name"):
            return R(self.name, "basics.name")
        if "postal" in q or "zip" in q or "pincode" in q or "pin code" in q:
            return R(self.postal, "location.postalCode")
        if "city" in q or "town" in q:
            return R(self.city, "location.city")
        if "state" in q or "province" in q or "region" in q:
            return R(self.region, "location.region")
        if "address" in q and "email" not in q:
            return R(self.address, "location.address_line1")
        if "country" in q:
            return R(self.country, "location.country")
        if "linkedin" in q:
            return R(self.linkedin, "profiles.linkedin")
        if "github" in q:
            return R(self.github, "profiles.github")
        if "portfolio" in q or "website" in q or "personal site" in q:
            return R(self.portfolio, "profiles.portfolio")
        if "current company" in q or "current employer" in q or ("employer" in q and "current" in q):
            return R(self.cur_company, "work[0].company")
        if "current title" in q or "job title" in q or "current role" in q or "current position" in q:
            return R(self.cur_title, "work[0].position")
        if "notice period" in q:
            d = self.prefs.get("notice_period_days")
            return (_num(d), "preferences.notice_period_days") if d else None
        if "expected" in q and ("ctc" in q or "salary" in q or "compensation" in q):
            return R(_num(self.prefs.get("expected_ctc")), "preferences.expected_ctc")
        if "current" in q and ("ctc" in q or "salary" in q):
            return R(_num(self.prefs.get("current_ctc")), "preferences.current_ctc")
        if "salary" in q or "compensation" in q or "ctc" in q:
            return R(_num(self.prefs.get("salary_expectation")), "preferences.salary_expectation")
        if "remote" in q:
            return R(self.prefs.get("remote_preference"), "preferences.remote_preference")
        if "year" in q:
            for sk, yrs in self.skill_years.items():
                if sk == "total_years":
                    continue
                if sk in q:
                    return (str(yrs), "skill_years." + sk)
            if "experience" in q and self.skill_years.get("total_years"):
                return (str(self.skill_years["total_years"]), "skill_years.total_years")
        return None

    def heal(self, label: str, intended: Optional[str], error_text: Optional[str], constraints: dict) -> Optional[str]:
        """Deterministic mechanical correction, or None → escalate to HITL."""
        opts: list[str] = constraints.get("options") or []
        ml = constraints.get("maxlength")
        field_type = (constraints.get("type") or "").lower()
        error = (error_text or "").lower()

        # Numeric field: strip currency symbols, units, commas → bare digits.
        # Only when the value actually looks numeric — never grep a digit out of prose.
        is_numeric = field_type in ("number", "tel") or any(
            w in error for w in ("decimal", "number", "numeric", "greater than", "must be")
        )
        if is_numeric and intended and _plausibly_numeric(intended):
            coerced = _num(intended)
            if coerced:
                return coerced

        # Value not in native <select> options → snap to the option.
        if opts and intended:
            norm_intended = _norm(intended)
            # exact match first
            for opt in opts:
                if _norm(opt) == norm_intended:
                    return opt
            # substring fallback — but ONLY when exactly one option matches. An
            # ambiguous partial ("San" → San Francisco / San Diego) escalates to a
            # human rather than guessing the wrong value into the form.
            partial = [opt for opt in opts if norm_intended and norm_intended in _norm(opt)]
            if len(partial) == 1:
                return partial[0]

        # maxlength exceeded → trim
        if ml and intended and len(intended) > int(ml):
            return intended[: int(ml)]

        # Open question / cannot determine → escalate
        return None


class HarnessState:
    def __init__(self, submit_policy: str = "hitl") -> None:
        self.tokens = 0
        self.flagged: list[str] = []
        self.per_field_status: dict[str, str] = {}  # label -> resolved|inferred|hitl|hitl_answered
        self.awaiting_human = False
        self.hitl_question: Optional[str] = None
        self.hitl_type: Optional[str] = None      # "field" | "review"
        self.hitl_field_label: Optional[str] = None
        self.hitl_options: Optional[list[str]] = None
        self.hitl_event = asyncio.Event()
        self.hitl_answer: Optional[str] = None
        self.submit_policy = submit_policy
        self.submitted = False  # True ONLY after a confirmed "application sent"
        self.needs_review: bool = False              # True if reconciler honest-stops a stuck run
        self.submit_approved = False  # approved to submit, not yet confirmed
        self.submit_skipped = False
        self.submit_summary: Optional[str] = None
        self.log_fn: Optional[LogFn] = None  # wired by run_apply

    def answer(self, value: str) -> None:
        self.hitl_answer = value
        self.awaiting_human = False
        self.hitl_event.set()

    def is_clean(self) -> bool:
        """True iff every field was resolved from profile with no inferred/unknown values."""
        return not any(s == "inferred" for s in self.per_field_status.values())

    async def wait_for_human(
        self,
        question: str,
        hitl_type: str = "field",
        field_label: Optional[str] = None,
        options: Optional[list[str]] = None,
    ) -> str:
        self.awaiting_human = True
        self.hitl_question = question
        self.hitl_type = hitl_type
        self.hitl_field_label = field_label
        self.hitl_options = options
        self.hitl_event.clear()
        self.hitl_answer = None
        if self.log_fn:
            self.log_fn(
                "hitl",
                f"[HITL {hitl_type.upper()}] {question[:120]}",
                hitl_type=hitl_type,
                field_label=field_label,
                options=options,
            )
        # step_timeout=1800 ensures this wait survives a long human pause
        await asyncio.wait_for(self.hitl_event.wait(), timeout=1800)
        answer = self.hitl_answer or ""
        self.hitl_answer = None
        return answer


def _llm() -> Any:
    provider = (os.getenv("LLM_PROVIDER") or "groq").strip().lower()
    if provider == "google":
        from browser_use import ChatGoogle
        return ChatGoogle(
            model=(os.getenv("GOOGLE_MODEL") or "gemini-2.5-flash").strip(),
            api_key=(os.getenv("GOOGLE_API_KEY") or "").strip(),
        )
    from browser_use import ChatGroq
    base = (os.getenv("GROQ_BASE_URL") or "https://api.groq.com").strip()
    return ChatGroq(
        model=(os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b").strip(),
        api_key=(os.getenv("GROQ_API_KEY") or "").strip(),
        base_url=base or None,
    )


async def _close_extra_tabs(cdp_url: str) -> None:
    """Close all browser tabs except the primary (oldest) one. Best-effort, never raises."""
    from browser_use import Browser
    browser = Browser(cdp_url=cdp_url)
    await browser.start()
    try:
        cdp = await browser.get_or_create_cdp_session()
        res = await cdp.cdp_client.send.Target.getTargets(
            params={}, session_id=cdp.session_id
        )
        targets = [t for t in (res.get("targetInfos") or []) if t.get("type") == "page"]
        for t in targets[1:]:  # keep targets[0] (primary)
            try:
                await cdp.cdp_client.send.Target.closeTarget(
                    params={"targetId": t["targetId"]}, session_id=cdp.session_id
                )
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            await browser.stop()
        except Exception:
            pass


async def _click_external_apply(cdp_url: str, job_url: str, log: "LogFn") -> bool:
    """Navigate to job_url and deterministically click the external apply button via CDP.

    Returns True if a button was found and clicked (new tab should be open).
    Returns False if no apply button found — caller should skip without running agent.
    Never raises.
    """
    from browser_use import Browser
    browser = Browser(cdp_url=cdp_url)
    await browser.start()
    try:
        await browser.navigate_to(job_url)
        await asyncio.sleep(2)  # let LinkedIn JS render the apply button
        cdp = await browser.get_or_create_cdp_session()
        js = """(function() {
    var candidates = Array.from(document.querySelectorAll('button, a')).filter(function(el) {
        var text = (el.textContent || '').trim().toLowerCase();
        var label = (el.getAttribute('aria-label') || '').toLowerCase();
        var isApply = text === 'apply' || label.indexOf('apply') !== -1 ||
                      text.indexOf('apply on company') !== -1 || text.indexOf('apply now') !== -1;
        var isEasyApply = text.indexOf('easy apply') !== -1 || label.indexOf('easy apply') !== -1;
        return isApply && !isEasyApply;
    });
    if (candidates.length > 0) { candidates[0].click(); return candidates[0].textContent.trim(); }
    return null;
})()"""
        res = await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": js, "returnByValue": True, "awaitPromise": False},
            session_id=cdp.session_id,
        )
        clicked = (res.get("result") or {}).get("value")
        if clicked:
            log("info", f"[EXT] Clicked apply button: '{str(clicked)[:60]}'")
            return True
        log("warn", "[EXT] No external apply button found on page")
        return False
    except Exception as e:  # noqa: BLE001
        log("warn", f"[EXT] click_external_apply failed: {e}")
        return False
    finally:
        try:
            await browser.stop()
        except Exception:
            pass


INSTRUCTION = """You are completing ONE LinkedIn Easy Apply application: {job_url}
The user is already logged in.

Work directly on the page. Do NOT maintain a todo list and do NOT use read_file / write_file /
replace_file — those waste steps. Take ONE action at a time and re-read the FRESH numbered
elements before each action (indexes change after every keystroke that opens a dropdown).

1. Go to the job URL and click the "Easy Apply" button to open the modal.
2. Fill EVERY field. Get each value with get_field_value(label) using the field's exact visible
   label/question. If it returns "UNKNOWN ...", follow that guidance.
3. Use the RIGHT tool for each control type:
   - Typeahead / autocomplete (city, location, country, school, company — typing pops up a
     suggestion list): call select_typeahead_option(index, value), then confirm the canonical
     label committed (e.g. "Bengaluru, Karnataka, India") before moving on.
   - Radio buttons / Yes-No / single-choice / native dropdown / checkbox / plain text or number:
     call set_field(label, value) with the question label and the value. set_field selects the
     right control directly — use it instead of click or select_dropdown for radio/choice questions.
   - Resume/CV: call get_field_value("resume") then upload_file with the returned path.
4. If you CANNOT fill or operate a field after one or two real attempts (e.g. an option you can't
   click), call ask_human_to_fill(field_label) — a human sets it in the viewer; then continue.
   Do not waste steps guessing CSS selectors or calling find_elements / evaluate / search_page.
5. Advance with Next / Continue / Review.
6. At the FINAL step with a "Submit application" button, do NOT click it yet — call
   request_submit_approval(summary). If approved, click "Submit application", wait for the on-screen
   "application was sent" confirmation, then call confirm_submitted(confirmation_text) passing the
   exact confirmation text you saw, then call done. If submission was skipped, just call done.
"""

INSTRUCTION_EXTERNAL = """You are applying for a job via the company's external career page. Job URL: {job_url}

Work directly on the page. Do NOT maintain a todo list, do NOT use read_file / write_file /
replace_file. Take ONE action at a time.

STEP 1 — Identify which page you are currently on:
  a) If you are on the LinkedIn job page (linkedin.com/jobs/...):
     - Scroll UP to the top of the page first. The "Apply" button is near the job title.
     - Click the Apply button (it may say "Apply", "Apply on company website", etc.).
       Do NOT click "Easy Apply". Do NOT click company name links or profile links.
     - The button opens a new tab. Switch to that new tab immediately.
  b) If you are already on a non-LinkedIn page (company career site / ATS):
     - You are in the right place. Proceed to STEP 2.

STEP 2 — Assess the company page (within your first 2 steps on it):
  - Not loaded (blank, error, infinite spinner): call skip_application("tab_not_loaded"), then done.
  - Requires creating a new account / sign-up wall: call skip_application("account_required"), then done.
  - Requires login and you are NOT already logged in: call skip_application("login_required"), then done.

STEP 3 — Fill the application form:
  - Scroll through the page to find all fields before filling.
  - Use get_field_value(label) to look up each value, then set_field(label, value) to fill it.
  - For resume / file upload: get_field_value("resume") gives the path; upload it.
  - If you cannot operate a field after one real attempt: call ask_human_to_fill(field_label).

STEP 4 — Navigate and submit:
  - Use Next / Continue / Submit buttons to advance through multi-step forms.
  - At the FINAL submit button: call request_submit_approval(summary). If approved, click submit,
    wait for the on-screen confirmation, then call confirm_submitted(confirmation_text), then done.

STEP 5 — Timeout: if you have not submitted after {max_steps} steps, call skip_application("timeout"), then done.
"""


async def _build_apply(
    cdp_url: str,
    job_url: str,
    log: LogFn,
    hstate: HarnessState,
    max_steps: int = 40,  # was 30: 1 action/step needs more steps for multi-field modals
    adapter=None,  # PlatformAdapter; None = no reconciliation
    easy_apply_only: bool = True,
) -> "tuple[Any, Any, int] | dict[str, Any]":
    """Build the configured browser-use Agent + reconcile callback for one job.

    Returns either a ready-to-run (agent, on_step_end, effective_steps) tuple, or a
    completed result dict when the job is skipped before the agent starts (external
    apply with no button). Shared by both run_apply (agent.run loop) and
    orchestrator.apply_graph.run_apply_graph (LangGraph take_step loop)."""
    from browser_use import Agent, Browser, Tools, ActionResult

    hstate.log_fn = log

    resolver = Resolver()
    tools = Tools()

    @tools.action("Get the value to enter for a form field, by its exact label/question text")
    async def get_field_value(label: str) -> "ActionResult":  # type: ignore[name-defined]
        low = label.lower()
        if "resume" in low or "cv" in low or ("upload" in low and "file" in low):
            log("field", f"resume field '{label[:50]}' -> {RESUME_PATH}")
            hstate.per_field_status[label] = "resolved"
            return ActionResult(
                extracted_content=f"Use the upload_file action with path: {RESUME_PATH}",
                include_in_memory=True,
            )

        norm_label = _norm(label)
        hit = resolver.resolve(label)
        if hit:
            value, source = hit
            log("field", f"resolved '{label[:60]}' -> '{value}' [{source}]")
            hstate.per_field_status[label] = "resolved"
            return ActionResult(extracted_content=value, include_in_memory=True)

        protected = _is_protected(norm_label)
        if protected:
            hstate.per_field_status[label] = "hitl"
            hstate.flagged.append(label)
            log("hitl", f"HITL (protected field): '{label[:80]}'")
            try:
                answer = await hstate.wait_for_human(
                    question=f'Please provide a value for: "{label}"',
                    hitl_type="field",
                    field_label=label,
                )
            except asyncio.TimeoutError:
                answer = ""
            if not answer:
                answer = "Decline to self-identify"
            hstate.per_field_status[label] = "hitl_answered"
            log("field", f"HITL answered '{label[:60]}' -> '{answer[:60]}'")
            return ActionResult(extracted_content=answer, include_in_memory=True)

        # Open free-text question with no profile answer → human, never fabricate prose.
        if _is_open_question(label):
            hstate.per_field_status[label] = "hitl"
            hstate.flagged.append(label)
            log("hitl", f"HITL (open question): '{label[:80]}'")
            try:
                answer = await hstate.wait_for_human(
                    question=f'Open question with no profile answer — how should I respond?\n\n"{label}"',
                    hitl_type="field",
                    field_label=label,
                )
            except asyncio.TimeoutError:
                answer = ""
            if answer:
                hstate.per_field_status[label] = "hitl_answered"
                log("field", f"HITL answered open question '{label[:50]}'")
                return ActionResult(extracted_content=answer, include_in_memory=True)
            return ActionResult(
                extracted_content="No answer provided — LEAVE BLANK if the field is optional; otherwise skip it.",
                include_in_memory=True,
            )

        # Non-protected factual unknown (years on a skill, yes/no eligibility): infer conservatively
        hstate.flagged.append(label)
        hstate.per_field_status[label] = "inferred"
        log("inferred", f"NOT in profile: '{label[:80]}' — agent answers conservatively")
        return ActionResult(
            extracted_content=(
                f"UNKNOWN (not in the candidate's profile). Candidate skills: {resolver.skills_hint}. "
                "Authorized to work in India (Indian citizen). Guidance: for a listed skill, give a "
                "matching years value (3-6); for an unlisted skill, answer 0-1; yes/no eligibility -> Yes "
                "unless it concerns visa/sponsorship OUTSIDE India (may require sponsorship); EEO -> "
                "'Decline to self-identify'. NEVER fabricate name/email/phone. Choose a sensible value."
            ),
            include_in_memory=True,
        )

    @tools.action(
        "Fill a city/location/school/company autocomplete (typeahead) field by element index "
        "and COMMIT the first matching suggestion. Use this for any field where typing pops up a "
        "suggestion list (LinkedIn location/city, country, school, company, degree). Do NOT use "
        "input_text+select_dropdown for these — they never commit the suggestion."
    )
    async def select_typeahead_option(index: int, text: str, browser_session) -> "ActionResult":  # type: ignore[name-defined]
        # Imported here (not at module top) so the EnhancedDOMTreeNode forward refs that
        # browser-use resolves via model_rebuild() at its own import are already in place.
        from browser_use.browser.events import (
            ClickElementEvent,
            SendKeysEvent,
            TypeTextEvent,
        )

        # Resolve the node FRESH at call time — indexes shift after every keystroke that
        # opens a dropdown, so we never trust a stale index handed in by the model.
        node = await browser_session.get_element_by_index(index)
        if node is None:
            log("warn", f"typeahead: index {index} not available (page changed)")
            return ActionResult(
                extracted_content=(
                    f"Element index {index} not available - page may have changed. "
                    "Re-read the numbered interactive elements and call select_typeahead_option again."
                ),
                include_in_memory=True,
            )

        # 1) Focus + clear + type the query in one event (clear=True focuses & clears first).
        #    Typing real key events is what fires LinkedIn's debounced suggestion fetch.
        ev = browser_session.event_bus.dispatch(TypeTextEvent(node=node, text=text, clear=True))
        await ev
        await ev.event_result(raise_if_any=True, raise_if_none=False)

        # 2) Wait for the async listbox to render (debounced fetch ~150-400ms; give it margin).
        await asyncio.sleep(0.8)

        # 3) Primary commit path: ArrowDown highlights option 0, Enter commits it. This drives
        #    the component's own selection handler and writes the canonical entity into its model.
        for key in ("ArrowDown", "Enter"):
            ke = browser_session.event_bus.dispatch(SendKeysEvent(keys=key))
            await ke
            await ke.event_result(raise_if_any=False, raise_if_none=False)
            await asyncio.sleep(0.15)

        # 4) Fallback: if the keyboard commit did not take, poll the DOM for a rendered option
        #    and click it directly. Read via CDP Runtime.evaluate against the stable selector.
        committed = True
        try:
            cdp_session = await browser_session.get_or_create_cdp_session()
            check = await cdp_session.cdp_client.send.Runtime.evaluate(
                params={
                    "expression": (
                        "(function(){var i=document.activeElement;"
                        "var v=i&&i.value?i.value:'';"
                        "var open=!!document.querySelector("
                        "'input[role=\\\"combobox\\\"][aria-expanded=\\\"true\\\"]');"
                        "return v+'||'+open;})()"
                    ),
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                session_id=cdp_session.session_id,
            )
            val = ((check.get("result") or {}).get("value") or "")
            field_value, still_open = (val.split("||", 1) + [""])[:2]
            # Not committed if the field still shows our raw partial text, or the list is still open.
            if still_open == "true" or field_value.strip().lower() == text.strip().lower():
                committed = False
        except Exception as e:  # CDP read is best-effort; fall through to click fallback
            log("warn", f"typeahead verify failed: {e}")
            committed = False

        if not committed:
            log("field", f"typeahead {text!r}: keyboard commit unconfirmed, trying click fallback")
            # Refresh the selector map so the freshly-rendered option gets an index, then click it.
            selector_map = await browser_session.get_selector_map()
            option_node = None
            for cand in selector_map.values():
                label = (getattr(cand, "attributes", None) or {}).get("aria-label", "") or ""
                role = (getattr(cand, "attributes", None) or {}).get("role", "") or ""
                cls = (getattr(cand, "attributes", None) or {}).get("class", "") or ""
                if role == "option" and (
                    "basic-typeahead__selectable" in cls
                    or text.strip().lower() in label.strip().lower()
                ):
                    option_node = cand
                    break
            if option_node is not None:
                ce = browser_session.event_bus.dispatch(ClickElementEvent(node=option_node))
                await ce
                await ce.event_result(raise_if_any=False, raise_if_none=False)
                await asyncio.sleep(0.2)

        log("field", f"typeahead {text!r} committed at index {index}")
        hstate.per_field_status[text] = "resolved"
        return ActionResult(
            extracted_content=(
                f"Committed typeahead suggestion for '{text}'. Verify the field now shows the full "
                "canonical label (e.g. 'City, Region, Country') before clicking Next/Continue."
            ),
            include_in_memory=True,
        )

    @tools.action("Call this INSTEAD of clicking Submit, when the application is ready to submit")
    async def request_submit_approval(summary: str) -> "ActionResult":  # type: ignore[name-defined]
        hstate.submit_summary = summary
        log("review", f"READY TO SUBMIT: {summary[:200]}")

        if hstate.submit_policy == "auto" or (
            hstate.submit_policy == "auto_if_clean" and hstate.is_clean()
        ):
            log("info", f"Submit policy: {hstate.submit_policy} — approved; agent submits + confirms")
            hstate.submit_approved = True
            return ActionResult(
                extracted_content=(
                    "Approved. Click the 'Submit application' button now, wait for LinkedIn's "
                    "confirmation that your application was sent, then call confirm_submitted. "
                    "Do NOT call confirm_submitted unless you actually see the sent confirmation."
                ),
                include_in_memory=True,
            )

        # HITL review — pause for human
        log("review", "Pausing for human review/approval before submit")
        try:
            answer = await hstate.wait_for_human(
                question=f"Application ready to submit.\n\nSummary: {summary[:400]}\n\nChoose: approve to submit, or skip.",
                hitl_type="review",
                options=["approve", "skip"],
            )
        except asyncio.TimeoutError:
            answer = "skip"
        if answer == "approve":
            log("info", "Human approved — agent submits + confirms")
            hstate.submit_approved = True
            return ActionResult(
                extracted_content=(
                    "Human approved. Click the 'Submit application' button now, wait for the "
                    "sent confirmation, then call confirm_submitted."
                ),
                include_in_memory=True,
            )
        log("info", "Human skipped this job")
        hstate.submit_skipped = True
        return ActionResult(
            extracted_content="Submission skipped by human. Call done without clicking Submit.",
            include_in_memory=True,
        )

    @tools.action(
        "Call after you SEE LinkedIn confirm the application was sent. Pass confirmation_text = "
        "the exact on-screen confirmation you saw (e.g. 'Your application was sent')."
    )
    async def confirm_submitted(confirmation_text: str) -> "ActionResult":  # type: ignore[name-defined]
        # ponytail: submitted flips true ONLY here. confirmation_text gives the action a real
        # parameter (a no-arg action makes browser-use inject a broken _placeholder field that
        # fails Gemini's schema validation with 53 errors).
        real_submit = True  # no-adapter path keeps original behavior
        if adapter is not None:
            try:
                real_submit = await adapter.is_submitted(agent)
            except Exception as e:  # noqa: BLE001
                log("warn", f"is_submitted raised: {e}")
                real_submit = False

        if real_submit:
            hstate.submitted = True
            hstate.needs_review = False  # a confirmed send supersedes any earlier stall flag
            log("info", f"✅ Submission CONFIRMED (application sent) — agent saw: {confirmation_text[:80]!r}")
            return ActionResult(extracted_content="Submission confirmed. Call done.", include_in_memory=True)

        # Auto-verify failed. Probe what the page actually shows, so we can fix is_submitted.
        snippet = ""
        try:
            bs = agent.browser_session
            cdp = await bs.get_or_create_cdp_session()
            r = await cdp.cdp_client.send.Runtime.evaluate(
                params={"expression": "document.body.innerText.slice(0,600)",
                        "returnByValue": True, "awaitPromise": True},
                session_id=cdp.session_id,
            )
            snippet = ((r.get("result") or {}).get("value") or "").replace("\n", " ")[:300]
        except Exception:
            pass
        log("warn", f"is_submitted=False; agent claims sent ({confirmation_text[:60]!r}); page reads: {snippet!r}")

        # HITL fallback: a human decides the truth rather than us recording a false negative.
        try:
            answer = await hstate.wait_for_human(
                question=(
                    "I clicked Submit and the agent reports it saw: "
                    f"'{confirmation_text[:200]}'. But I could NOT auto-verify the application was sent.\n\n"
                    "Did it actually submit? Choose: approve = yes it was sent, skip = no / not sent."
                ),
                hitl_type="review",
                options=["approve", "skip"],
            )
        except asyncio.TimeoutError:
            answer = ""

        # Only an explicit "approve" marks submitted — any other answer fails closed to
        # needs_review. The review UI MUST present this as approve/skip (hitl_options is set);
        # a free-text "yes" would be silently downgraded, never up-graded to a false submit.
        if answer == "approve":
            hstate.submitted = True
            hstate.needs_review = False
            log("info", "✅ Submission CONFIRMED by human")
            return ActionResult(extracted_content="Human confirmed submission. Call done.", include_in_memory=True)

        hstate.needs_review = True  # unverified — never record as submitted
        log("warn", "Submission NOT confirmed (human skipped / timeout) — marking needs_review")
        return ActionResult(
            extracted_content="Submission could not be confirmed. Call done (do not retry submit).",
            include_in_memory=True,
        )

    @tools.action(
        "Set a NON-typeahead field by its question label: radio (Yes/No / single-choice), native "
        "dropdown, checkbox, or plain text/number. Use this INSTEAD of click/select_dropdown for "
        "radio buttons and choice questions — it sets the right control directly even when the option "
        "has no clickable index. Pass label=the question text, value=the option text/value to set."
    )
    async def set_field(label: str, value: str, browser_session) -> "ActionResult":  # type: ignore[name-defined]
        import json as _json
        try:
            cdp = await browser_session.get_or_create_cdp_session()
            expr = _SET_FIELD_JS.replace("__LABEL__", _json.dumps(label)).replace("__VALUE__", _json.dumps(value))
            r = await cdp.cdp_client.send.Runtime.evaluate(
                params={"expression": expr, "returnByValue": True, "awaitPromise": True},
                session_id=cdp.session_id,
            )
            res = _json.loads(((r.get("result") or {}).get("value")) or "{}")
        except Exception as e:  # noqa: BLE001
            log("warn", f"set_field error '{label[:50]}': {e}")
            return ActionResult(
                extracted_content=f"set_field failed ({e}). If you can't set this field, call ask_human_to_fill.",
                include_in_memory=True,
            )
        if res.get("ok"):
            log("field", f"set_field '{label[:50]}' = '{value[:40]}' [{res.get('type')}]")
            return ActionResult(
                extracted_content=f"Set '{label[:60]}' = '{value}'. Re-read the fresh page state, then continue.",
                include_in_memory=True,
            )
        # Miss — log the probe data (real markup) so the selector can be fixed.
        log("review", f"set_field MISS '{label[:50]}'='{value[:30]}': {str(res)[:280]}")
        return ActionResult(
            extracted_content=(
                f"Could not auto-set the field ({res.get('error')}). Try once more with the exact option "
                f"text, or call ask_human_to_fill('{label[:60]}')."
            ),
            include_in_memory=True,
        )

    @tools.action(
        "Call when you CANNOT fill or operate a field after a real attempt (e.g. a radio/option you "
        "can't click, a control with no index). A human sets it in the interactive viewer, then you "
        "continue. Pass the field's label/question."
    )
    async def ask_human_to_fill(field_label: str) -> "ActionResult":  # type: ignore[name-defined]
        hstate.per_field_status[field_label] = "hitl"
        if field_label not in hstate.flagged:
            hstate.flagged.append(field_label)
        log("review", f"Stuck on '{field_label[:80]}' — handing to human")
        try:
            answer = await hstate.wait_for_human(
                question=(
                    f'I cannot operate this field automatically:\n\n"{field_label}"\n\n'
                    "Please set it in the interactive viewer, then choose: approve = I set it (continue), "
                    "skip = leave it."
                ),
                hitl_type="field",
                field_label=field_label,
                options=["approve", "skip"],
            )
        except asyncio.TimeoutError:
            answer = "skip"
        if answer == "skip":
            hstate.per_field_status[field_label] = "skipped"
            return ActionResult(
                extracted_content="Field skipped by human. Continue with the rest of the form.",
                include_in_memory=True,
            )
        hstate.per_field_status[field_label] = "hitl_answered"
        return ActionResult(
            extracted_content="Human set the field in the viewer. Re-read the FRESH page state and continue.",
            include_in_memory=True,
        )

    @tools.action(
        "Signal that this application cannot be completed automatically. "
        "Call before done(). reason must be one of: account_required | login_required | tab_not_loaded | timeout"
    )
    async def skip_application(reason: str) -> "ActionResult":  # type: ignore[name-defined]
        hstate.submit_skipped = True
        log("info", f"[SKIP] {reason}")
        return ActionResult(
            extracted_content=f"Application skipped: {reason}. Now call done().",
            include_in_memory=True,
        )

    def on_decision(browser_state: Any, model_output: Any, n_steps: int) -> None:
        try:
            judgement = getattr(model_output, "evaluation_previous_goal", "") or ""
            next_goal = getattr(model_output, "next_goal", "") or ""
            actions = []
            for a in (getattr(model_output, "action", None) or []):
                try:
                    actions.append(a.model_dump(exclude_none=True))
                except Exception:
                    actions.append(str(a))
            url = getattr(browser_state, "url", "") or ""
            log(
                "decision",
                f"step {n_steps} | {next_goal[:90]}  tab={url}",
                url=url,
                planned_actions=actions,
            )
        except Exception as e:
            log("warn", f"decision-log error: {e}")

    async def on_step_end(agent: Any) -> None:
        try:
            res = getattr(getattr(agent, "state", None), "last_result", None) or []
            last = res[-1] if res else None
            if last is not None:
                err = getattr(last, "error", None)
                content = getattr(last, "extracted_content", None)
                try:
                    cur_url = getattr(getattr(agent, "browser_session", None), "current_url", "") or ""
                except Exception:
                    cur_url = ""
                msg = ('ERROR ' + str(err)) if err else (str(content)[:120] if content else 'ok')
                log(
                    "result",
                    f"step result: {msg}  tab={cur_url}" if cur_url else f"step result: {msg}",
                )
            uh = getattr(getattr(agent, "token_cost_service", None), "usage_history", None)
            if uh is not None:
                hstate.tokens = len(uh)
        except Exception:
            pass

        if adapter is not None:
            from reconciler import reconcile
            try:
                await reconcile(agent, hstate, resolver, adapter, log)
            except Exception as e:
                log("warn", f"reconcile error: {e}")

    # Tab hygiene: close any tabs left over from a previous job
    await _close_extra_tabs(cdp_url)

    # Select apply mode
    if easy_apply_only:
        instruction = INSTRUCTION.format(job_url=job_url)
        effective_steps = max_steps
    else:
        effective_steps = min(max_steps, 20)  # cap external jobs — bail faster if stuck
        # Deterministic pre-click: find and click the apply button before the agent starts.
        # The LLM should never hunt for the button — it's always at the top of the LinkedIn panel.
        clicked = await _click_external_apply(cdp_url, job_url, log)
        if not clicked:
            hstate.submit_skipped = True
            log("info", "[SKIP] no_apply_button")
            return {
                "status": "skipped",
                "llm_calls": 0,
                "tokens": {"prompt": 0, "completion": 0, "total": 0, "cost": 0.0},
                "fields_not_in_profile": [],
                "per_field_status": {},
                "submitted": False,
                "submit_skipped": True,
                "is_clean": True,
                "needs_review": False,
            }
        await asyncio.sleep(1)  # let the new tab settle before agent starts
        instruction = INSTRUCTION_EXTERNAL.format(job_url=job_url, max_steps=effective_steps)

    mode = "easy_apply" if easy_apply_only else "external"
    log("info", f"[JOB] mode={mode} steps={effective_steps} url={job_url}")
    log("info", f"Resume at (container path): {RESUME_PATH}")

    browser = Browser(cdp_url=cdp_url)
    agent = Agent(
        task=instruction,
        llm=_llm(),
        browser=browser,
        tools=tools,
        use_vision=False,
        flash_mode=False,
        calculate_cost=True,  # populate the token meter (get_usage_summary) for the logs
        available_file_paths=[RESUME_PATH],
        register_new_step_callback=on_decision,
        step_timeout=1800,  # generous: allows HITL waits inside a step
        # 1 action/step forces a fresh DOM re-index before every action, so a typeahead
        # opening the dropdown can't leave later batched actions running on stale indexes.
        max_actions_per_step=1,
    )
    return agent, on_step_end, effective_steps


async def _finalize_apply(agent: Any, hstate: HarnessState, status: Any, log: LogFn) -> dict[str, Any]:
    """Build the run result dict from the finished agent + harness state.

    Shared tail of both apply engines: reads browser-use's token meter (best-effort)
    and maps the harness flags into the outcome dict app._classify_outcome consumes."""
    tok = {"prompt": 0, "completion": 0, "total": 0, "cost": 0.0}
    try:
        summ = await agent.token_cost_service.get_usage_summary()
        tok = {
            "prompt": summ.total_prompt_tokens,
            "completion": summ.total_completion_tokens,
            "total": summ.total_tokens,
            "cost": round(summ.total_cost, 4),
        }
    except Exception as e:  # noqa: BLE001
        log("warn", f"token usage unavailable: {e}")

    out: dict[str, Any] = {
        "status": status or "completed",
        "llm_calls": hstate.tokens,
        "tokens": tok,
        "fields_not_in_profile": hstate.flagged,
        "per_field_status": hstate.per_field_status,
        "submitted": hstate.submitted,
        "submit_skipped": hstate.submit_skipped,
        "is_clean": hstate.is_clean(),
        "needs_review": hstate.needs_review,
    }
    log(
        "info",
        f"apply finished: {out['status']} | {hstate.tokens} llm calls | "
        f"tokens {tok['total']:,} ({tok['prompt']:,} in / {tok['completion']:,} out) "
        f"${tok['cost']:.4f} | {len(hstate.flagged)} field(s) not in profile",
    )
    if hstate.flagged:
        log(
            "flag-missing",
            "Fields the profile could not answer: " + "; ".join(hstate.flagged[:15]),
        )
    return out


async def run_apply(
    cdp_url: str,
    job_url: str,
    log: LogFn,
    hstate: HarnessState,
    max_steps: int = 40,
    adapter=None,
    easy_apply_only: bool = True,
) -> dict[str, Any]:
    """Legacy apply engine: browser-use's Agent.run() owns the step loop; the reconciler
    runs via the on_step_end hook. Unchanged behavior — kept as the default and fallback."""
    built = await _build_apply(cdp_url, job_url, log, hstate, max_steps, adapter, easy_apply_only)
    if isinstance(built, dict):
        return built  # external pre-click skip
    agent, on_step_end, effective_steps = built
    log("info", "Agent starting (profile NOT inlined — values pulled per-field from profile.yaml)")
    result = await agent.run(max_steps=effective_steps, on_step_end=on_step_end)
    await _close_extra_tabs(cdp_url)
    return await _finalize_apply(agent, hstate, getattr(result, "status", None), log)
