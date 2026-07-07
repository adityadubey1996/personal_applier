"""Integration test for the adapter/reconciler wiring activated in app.py.

Proves the two things the unit tests (which run components in isolation) could
not catch — the reason the unwired state went unnoticed:

1. app.py's outcome classifier surfaces an unresolved reconciler stall as
   'needs_review' instead of silently counting it submitted/skipped.
2. reconcile(), driven with the REAL LinkedInAdapter + REAL Resolver, mechanically
   heals an aria-invalid numeric field whose intended value is unknown — it
   coerces the rejected on-page value ('₹30 LPA' -> '30') and feeds it back via
   agent.add_new_task. This is the live path app.py now triggers.
"""
import asyncio
import sys

sys.path.insert(0, str(__file__).rsplit("/tests/", 1)[0])  # add v5/backend to path

import app
import reconciler
from apply_harness import HarnessState, Resolver
from platforms.linkedin import LinkedInAdapter


def test_classify_outcome_branches():
    # submitted wins over everything
    assert app._classify_outcome(True, True, True, "completed") == "submitted"
    # explicit human skip
    assert app._classify_outcome(False, True, False, "completed") == "skipped"
    # unresolved reconciler stall -> needs_review (NOT submitted)
    assert app._classify_outcome(False, False, True, "completed") == "needs_review"
    # nothing flagged -> raw agent status
    assert app._classify_outcome(False, False, False, "failed") == "failed"
    assert app._classify_outcome(False, False, False, "") == "completed"
    print("test_classify_outcome_branches passed")


class _FakeAgent:
    """Minimal stand-in for a browser-use Agent: captures add_new_task injections."""
    def __init__(self):
        self.injected: list[str] = []
        self.task = "https://www.linkedin.com/jobs/view/123/"

        class _State:
            url = "https://www.linkedin.com/jobs/view/123/"
            n_steps = 7
            last_result: list = []
        self.state = _State()

    def add_new_task(self, text: str) -> None:
        self.injected.append(text)


async def test_reconcile_heals_aria_invalid_with_real_adapter():
    """Real LinkedInAdapter + real Resolver: an aria-invalid '₹30 LPA' numeric
    field is healed to '30' and injected, with no human escalation and no stall."""
    original = reconciler._cdp_evaluate

    async def mock_cdp_evaluate(agent, expression):
        if "(function" in expression:  # the aria-invalid scan
            return [{
                "label": "Current salary (INR)",
                "value": "₹30 LPA",
                "errorText": "Enter a decimal number larger than 0.0",
                "constraints": {"type": "number", "options": [], "maxlength": None},
            }]
        return ""  # page-signature read

    reconciler._cdp_evaluate = mock_cdp_evaluate
    try:
        agent = _FakeAgent()
        hstate = HarnessState()
        resolver = Resolver()
        adapter = LinkedInAdapter()

        await reconciler.reconcile(agent, hstate, resolver, adapter, lambda *a, **k: None)

        assert len(agent.injected) == 1, f"expected one corrective task, got {agent.injected}"
        msg = agent.injected[0]
        assert "30" in msg, f"healed value '30' missing from: {msg!r}"
        assert "Current salary" in msg, f"field label missing from: {msg!r}"
        assert "₹" not in msg, f"currency symbol should be stripped, got: {msg!r}"
        # mechanical heal resolved it -> not an honest-stop
        assert hstate.needs_review is False, "mechanical heal must not flag needs_review"
        print("test_reconcile_heals_aria_invalid_with_real_adapter passed")
    finally:
        reconciler._cdp_evaluate = original


async def main():
    test_classify_outcome_branches()
    await test_reconcile_heals_aria_invalid_with_real_adapter()
    print("All tests passed")


if __name__ == "__main__":
    asyncio.run(main())
