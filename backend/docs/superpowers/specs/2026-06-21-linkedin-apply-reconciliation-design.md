# Job-Apply — Reconciliation Core + Platform Adapters

**Date:** 2026-06-21
**Status:** Design approved (Core + Adapter); spec under review (pre-implementation)
**Component:** `v5/backend` (apply harness)
**Revision:** 2 — generalized to a platform-agnostic core with per-platform adapters.

---

## 1. Problem

The Easy Apply agent (browser-use + LLM) fails on an open-ended set of form
anomalies: typeaheads that never commit, numeric fields that reject `₹30 LPA` /
`40 days`, and a status bug that records "submitted" before any real submission.
All share one root cause:

> **The Actor acts, *assumes* success, and advances. Nothing compares what we
> intended to put on the page against what the page actually accepted.**

Patching one field type at a time does not scale — and we will eventually apply on
**multiple platforms** (LinkedIn now; Workday / Greenhouse / Lever / Naukri /
Indeed later). We need a *process* that handles the divergence-recovery class
once, platform-agnostically, with platform specifics isolated.

## 2. Goals / Non-goals

**Goals**
- A closed loop that detects when the page rejected our input (or the agent is
  stuck) and resolves it — heal deterministically, or escalate to a human.
- **Build the recovery core once**; a new platform = one thin adapter, core
  untouched.
- Honesty: never record a submission that did not happen; never send fabricated
  data to an employer.
- Minimal footprint: the core hangs off an existing seam; the Actor/Supplier
  barely change. Reduce prompt size, not grow it.
- Anomalies become structured data (a backlog / test set), not log-archaeology.

**Non-goals (deferred)**
- Adapters for any platform other than LinkedIn (design the seam, build one).
- Multi-page / wizard navigation logic beyond what the Actor already does.
- ML/heuristic answer inference for unknown employer questions (escalate instead).
- A proactive per-step validation tax (engage only on divergence/stall).

## 3. Mental model

One application = a control loop with three responsibilities, each owning one
thing:

| Layer | Owns | Lives in |
|---|---|---|
| **Actor** | *Make progress* — read page, decide, fill, click, navigate | browser-use `Agent` + `tools` (`run_apply`) |
| **Supplier** | *Ground truth about the candidate* — "what value goes here" | `Resolver` / `get_field_value` |
| **Reconciler** | *Keep observed reality in agreement with intent* | **new** — see §4 |

Like Terraform/Kubernetes: a **desired state** (values we tried to set, tracked via
`get_field_value` / `hstate.per_field_status`) and an **observed state** (what the
page shows now). A **divergence** is any field where observed ≠ intended, or
observed = error. Reconciliation closes divergences.

## 4. Platform abstraction — Core + Adapter

The Reconciler splits into a platform-agnostic **Core** and a per-platform
**Adapter**. The Core is the thing we build once and keep; adapters are what we
"build more over it."

```
        ┌──────────────────────────────────────────┐
        │   Reconciliation Core   (build ONCE)       │  platform-agnostic;
        │   observe · classify · heal(HTML/ARIA std) │  keys off W3C/ARIA
        │   escalate · honest-stop · anomaly log      │  (every web form has these)
        └───────────────────┬────────────────────────┘
                            │ asks the adapter only for platform specifics
        ┌───────────────────▼────────────────────────┐
        │   PlatformAdapter   (build per platform)    │  thin: ~5 members
        └─────────────────────────────────────────────┘
          LinkedInAdapter (now)  ·  WorkdayAdapter (later)  ·  …
```

**Why the Core is platform-agnostic by construction:** it reconciles against
**W3C/ARIA standards** — `aria-invalid`, `required`, `pattern`, `min`/`max`,
`maxlength`, `<select><option>`, `role="combobox"` — which every standards-based
form (LinkedIn, Workday, Greenhouse, Lever, Indeed, Naukri) exposes. It does not
read LinkedIn markup.

**Division of responsibility**

| Concern | Core (agnostic) | Adapter (per platform) |
|---|---|---|
| detect aria-invalid / inline error / stall / repetition | ✅ | — |
| heal numeric / option-snap / maxlength-trim | ✅ | — |
| escalate to HITL / honest-stop / anomaly log | ✅ | — |
| commit a typeahead (which option selector?) | drives the loop | `typeahead_selector` |
| "is the application actually submitted?" | calls adapter | `is_submitted()` |
| open the apply flow (Easy Apply button, etc.) | — | `entry()` |
| field-label quirks / hints | — | `field_hints` (optional) |
| discover jobs (search / scrape ids) | — | `discover_jobs()` |

**Honesty caveat (no over-claiming):** the Core covers the *recovery/correctness*
concern — the part that repeatedly breaks — on any standards-based form. It does
**not** cover *navigation/flow* differences (LinkedIn modal vs Workday's multi-page
wizard + account creation vs Indeed's off-site redirect). Flow is handled by the
**Actor (LLM)** driven by a per-platform task prompt + the adapter's hints. No
single component does everything; the layer that hurts most is built once.

## 5. Control loop

```
Actor step (browser-use fills/clicks/navigates)
      │
      ▼
 on_step_end ──► Core.reconcile(adapter)
      │                │
      │          divergence or stall?
      │          ├─ no  ─► continue (Actor runs free)
      │          └─ yes ─► classify each divergence ──┐
      │                                               │
      │   mechanical ─► heal() ─► inject corrected value via agent.add_new_task()
      │   judgment   ─► HITL ask (wait_for_human) ─► inject human answer
      │   unresolved + stall persists ─► stop job honestly + log anomaly
      ▼
```

Engagement is **recovery-only** — the Core stays dormant while the Actor makes
progress; it wakes only on a divergence/stall signal (§6).

## 6. Detection (Core) — what wakes the loop

Cheap checks, only inside `on_step_end` (no per-step tax beyond one small DOM read):

1. **Action error** — last `ActionResult` has `error` / "index not available".
2. **Field rejected** — a field we touched now has `aria-invalid="true"` or an
   adjacent inline error node. browser-use already serializes `aria-invalid`,
   `required`, `min`, `max`, `pattern` (`dom/views.py` `DEFAULT_INCLUDE_ATTRIBUTES`);
   inline error *text* is read via one CDP `Runtime.evaluate` over filled fields.
3. **Stall** — a step-signature hash (`url + progress% + visible field
   labels/values`) unchanged for `K` consecutive steps (default `K=3`).
4. **Repetition** — reuse browser-use's existing loop counter (it already emits
   `🔁 Loop detection nudge`); do not reinvent it.

All four are platform-agnostic (HTTP/HTML/ARIA + the Actor's own state).

## 7. Resolution ladder (Core)

For each divergence, in order:

1. **Re-read reality** — current value, inline error text, and constraints
   (`type`, `min`/`max`/`pattern`/`maxlength`, `<option>` list).
2. **Heal — mechanical only** (deterministic, HTML/ARIA-standard):
   | Cause | Heal |
   |---|---|
   | numeric field rejecting `₹`/units/commas | `_num(intended)` → bare digits |
   | value not in a native `<select>` | snap to closest real `<option>` |
   | typeahead not committed | commit via `adapter.typeahead_selector` (the existing `select_typeahead_option` path) |
   | value exceeds `maxlength` | trim |

   The corrected value is fed back via `agent.add_new_task(...)` — public/supported
   (`agent/service.py:985`), injecting a `<follow_up_user_request>` seen next step.
3. **Escalate — judgment calls** (missing value, open employer question): pause via
   `hstate.wait_for_human(...)`, inject the human's answer. **The harness never
   invents a value that gets submitted.**
4. **Stop honestly** — stall persists and a divergence is unresolvable (no human
   within timeout) → end the job marked `needs_review` (not submitted) + log.

When unsure whether a divergence is mechanical, classify as **judgment** and
escalate. A wrong "mechanical" heal that auto-submits is the worst failure mode.

## 8. Honesty (mostly already in place)

- `hstate.submitted` flips `True` **only** inside `confirm_submitted`, which the
  Actor calls after the adapter's `is_submitted()` signal is observed. (Action
  exists; tie it to `adapter.is_submitted()`.)
- HITL timeout / unresolved divergence → outcome `needs_review`, **not**
  `submitted`. `app.py` records the true outcome.

## 9. Anomaly log (Core)

One JSONL row per escalation or honest-stop, appended to `<data>/anomalies.jsonl`:

```json
{"ts":"...","platform":"linkedin","job_url":"...","step":14,
 "field":"current net salary in INR (Monthly)","intended":"₹30 LPA",
 "observed":"Enter a decimal number larger than 0.0",
 "class":"mechanical|judgment","resolution":"healed|escalated|skipped"}
```

This file *is* the cross-platform anomaly backlog and seed test set.

## 10. What gets DELETED (anti-pollution)

The Core enforces validation deterministically, so remove the prompt patches added
earlier: the "check each field for a validation error before Next" step and the
"NUMERIC fields … digits only" rule. The Supplier already emits bare numbers for
*known* numeric fields (`_num` on salary/notice); the Core catches the surprises.
Net: shorter prompt, one focused core, Actor/Supplier almost untouched.

Kept: `confirm_submitted`, `_num` (reused by healer), `select_typeahead_option`
(now driven via the adapter selector), `max_actions_per_step=1`.

## 11. Components & interfaces

**New — `v5/backend/reconciler.py` (Core, platform-agnostic)**
```python
@dataclass
class Divergence:
    field_label: str
    intended: str | None
    observed: str | None
    error_text: str | None
    constraints: dict        # {type, min, max, pattern, maxlength, options:[...]}
    kind: str                # "mechanical" | "judgment" | "unknown"

async def observe(agent, hstate) -> tuple[list[Divergence], bool]:
    """Read page state; return (divergences, stalled?). Pure read — no mutation."""

def classify(div, resolver) -> str:
    """'mechanical' if resolver.heal can return a value, else 'judgment'."""

async def reconcile(agent, hstate, resolver, adapter, log) -> None:
    """Called from on_step_end. observe → classify → heal/escalate/stop+log."""
```

**New — `v5/backend/platforms/base.py` (PlatformAdapter interface)**
```python
class PlatformAdapter(Protocol):
    name: str                                   # "linkedin"
    typeahead_selector: str                     # CSS for committed-option detection
    field_hints: dict[str, str]                 # optional label → canonical hint

    async def entry(self, agent) -> None:       # open the apply flow
    async def is_submitted(self, agent) -> bool # detect the real "sent" signal
    async def discover_jobs(self, ctx, n) -> list[str]   # search/scrape job ids
```

**New — `v5/backend/platforms/linkedin.py` (LinkedInAdapter)**
- `typeahead_selector = "[role=option].basic-typeahead__selectable"`
- `is_submitted` = detect "application was sent" confirmation modal/text.
- `entry` = click "Easy Apply".
- `discover_jobs` = the deterministic job-id scrape (move from `app.py`/`_search_jobs`).

**Supplier — `Resolver` (extend)**
```python
def heal(self, label, intended, error_text, constraints) -> str | None:
    """Deterministic correction (numeric/option/maxlength), or None → escalate."""
```

**Edit — `run_apply`**: pass an `adapter` into the run; the existing `on_step_end`
(currently logs only) calls `await reconcile(agent, hstate, resolver, adapter, log)`.

**State — `HarnessState` (extend)**: `last_signature`, `unchanged_count`,
`needs_review`.

## 12. First-build scope (YAGNI)

Build: the Core (`observe`/`classify`/`heal`/escalate/stop/log) + the
`PlatformAdapter` interface + **one** `LinkedInAdapter` + delete the two prompt
patches. Do **not** write Workday/Indeed/etc. adapters — the seam makes them
additive when a real second platform arrives.

## 13. Testing (one runnable check per non-trivial unit)

- `test_heal.py` — `Resolver.heal` coerces `"₹30 LPA"→"30"`, snaps a near-miss to a
  real `<option>`, trims to `maxlength`, returns `None` for an open question.
- `test_reconciler.py` — with a faked page state + a **stub adapter**, one
  `aria-invalid` field yields one mechanical `Divergence`; `reconcile` calls
  `add_new_task` on a mock agent; stall counter trips at `K`. No live browser —
  proves the Core is adapter-independent.
- `test_linkedin_adapter.py` — `is_submitted` true/false on fixture HTML;
  `typeahead_selector` matches a committed option, not the raw input.

## 14. Model config (separable)

Default the apply agent to Gemini 2.5 Flash (it acts on the validation signal
gpt-oss ignored). In `v5/backend/.env`:
```
LLM_PROVIDER=google
GOOGLE_API_KEY=<key>
GOOGLE_MODEL=gemini-2.5-flash   # already the default when provider=google
```
Optional: flip the code default `groq → google` in `_llm`/`_make_llm`/`_llm_config`.

## 15. Risks / assumptions

- **Heal/escalate boundary stays conservative** — ambiguity → escalate.
- **`add_new_task` lag is one step** — correction lands next step; fine for
  recovery-only.
- **Adapter surface creep** — keep adapters to the ~5 members above; if a platform
  needs more, that signals new *Core* capability (e.g. a wizard-progress probe),
  added once and shared — not per-platform sprawl.
- **Gemini still required** — the loop feeds corrections back as text; a model too
  weak to act on them re-stalls.
- **Stall hash tuning** — `K` and signature fields need tuning against real runs;
  start `K=3`.
