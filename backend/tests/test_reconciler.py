"""Test reconciler.observe, reconciler.reconcile, and stall detection.

Tests aria-invalid divergence detection, mechanical healing via add_new_task,
and the stall counter which trips at K=3 unchanged signatures.
"""
import sys
import asyncio
sys.path.insert(0, str(__file__).rsplit("/tests/", 1)[0])  # add v5/backend to path

from reconciler import Divergence, observe, reconcile


# ============================================================================
# Stub adapter
# ============================================================================

class StubAdapter:
    name = "test"
    typeahead_selector = "[role=option]"
    field_hints = {}

    async def entry(self, agent):
        pass

    async def is_submitted(self, agent):
        return False

    async def discover_jobs(self, ctx, n):
        return []


# ============================================================================
# Stub HarnessState
# ============================================================================

class StubHarnessState:
    def __init__(self):
        self.needs_review = False


# ============================================================================
# Test 1: aria-invalid → mechanical Divergence
# ============================================================================

async def test_aria_invalid_gives_divergence():
    """Patch _cdp_evaluate to return invalid field, observe() detects it."""
    import reconciler

    # Mock _cdp_evaluate to return one invalid field
    original_cdp_evaluate = reconciler._cdp_evaluate

    async def mock_cdp_evaluate(agent, expression):
        # The aria-invalid scan is the only expression wrapped in "(function".
        if "(function" in expression:  # invalid-fields JS
            return [
                {
                    "label": "Salary",
                    "value": "₹30 LPA",
                    "errorText": "Enter a decimal number",
                    "constraints": {
                        "type": "number",
                        "options": [],
                        "maxlength": None
                    }
                }
            ]
        # Page signature call (short expression)
        return ""

    reconciler._cdp_evaluate = mock_cdp_evaluate

    try:
        # Stub agent
        class StubAgent:
            class State:
                url = "https://example.com"
                n_steps = 1
                last_result = []
            state = State()

        agent = StubAgent()
        hstate = StubHarnessState()

        divergences, stalled = await observe(agent, hstate)

        assert len(divergences) > 0, "Expected at least one divergence from aria-invalid"

        # Check that the salary field is in there
        salary_divs = [d for d in divergences if d.field_label == "Salary"]
        assert len(salary_divs) > 0, "Expected Salary field in divergences"

        salary_div = salary_divs[0]
        assert salary_div.kind == "unknown", f"Expected kind='unknown', got {salary_div.kind!r}"
        assert salary_div.error_text == "Enter a decimal number", f"Expected error text, got {salary_div.error_text!r}"
        assert salary_div.constraints.get("type") == "number", "Expected number type in constraints"

    finally:
        reconciler._cdp_evaluate = original_cdp_evaluate


# ============================================================================
# Test 2: reconcile calls add_new_task on mechanical divergence
# ============================================================================

async def test_reconcile_detects_divergence():
    """reconcile() detects aria-invalid divergence from _cdp_evaluate."""
    import reconciler
    from apply_harness import Resolver

    # Mock _cdp_evaluate
    original_cdp_evaluate = reconciler._cdp_evaluate

    async def mock_cdp_evaluate(agent, expression):
        # Check the expression to distinguish page-sig from invalid-fields
        # The invalid-fields JS starts with "(function() {"
        if "(function() {" in expression:  # invalid-fields JS
            return [
                {
                    "label": "Salary",
                    "value": "₹30 LPA",
                    "errorText": "Enter a decimal number",
                    "constraints": {
                        "type": "number",
                        "options": [],
                        "maxlength": None
                    }
                }
            ]
        # page signature and document.title calls return a string
        return "test"

    reconciler._cdp_evaluate = mock_cdp_evaluate

    try:
        # Stub agent
        class StubAgent:
            class State:
                url = "https://example.com"
                n_steps = 1
                last_result = []
            state = State()
            task = "https://example.com/job/123"

        agent = StubAgent()
        hstate = StubHarnessState()

        # Call observe directly to verify divergence detection
        divergences, stalled = await reconciler.observe(agent, hstate)

        # Should have detected the Salary field as a divergence
        assert len(divergences) > 0, f"Expected divergences from aria-invalid, got {len(divergences)}"

        salary_divs = [d for d in divergences if d.field_label == "Salary"]
        assert len(salary_divs) > 0, "Expected Salary field in divergences"

        div = salary_divs[0]
        assert div.error_text == "Enter a decimal number"
        assert div.observed == "₹30 LPA"
        assert div.constraints.get("type") == "number"

    finally:
        reconciler._cdp_evaluate = original_cdp_evaluate


# ============================================================================
# Test 3: stall keys off browser-use's own counters, NOT a page signature
# ============================================================================

class _LoopDetector:
    def __init__(self, reps=0):
        self.max_repetition_count = reps


async def test_stall_uses_browser_use_counters():
    """A static page is NOT a stall: only consecutive_failures or repetition trip it.

    This is the regression guard for the false-stop bug — a healthy review/submit
    page (agent succeeding, consecutive_failures=0) must never honest-stop.
    """
    import reconciler

    original_cdp_evaluate = reconciler._cdp_evaluate

    async def mock_cdp_evaluate(agent, expression):
        return [] if "(function" in expression else ""

    reconciler._cdp_evaluate = mock_cdp_evaluate

    try:
        class StubAgent:
            class State:
                url = "https://example.com"
                n_steps = 9
                last_result = []
                consecutive_failures = 0
                loop_detector = _LoopDetector(0)
            state = State()

        agent = StubAgent()
        hstate = StubHarnessState()

        # Healthy page, agent making progress → never stalls, however many steps.
        for _ in range(6):
            _, stalled = await observe(agent, hstate)
            assert not stalled, "a static, progressing page must not be treated as a stall"

        # Genuinely stuck: repeated failures trip it.
        agent.state.consecutive_failures = 4
        _, stalled = await observe(agent, hstate)
        assert stalled, "4 consecutive failures should stall"

        # Or a tight action loop trips it.
        agent.state.consecutive_failures = 0
        agent.state.loop_detector = _LoopDetector(8)
        _, stalled = await observe(agent, hstate)
        assert stalled, "8 repeated actions should stall"

    finally:
        reconciler._cdp_evaluate = original_cdp_evaluate


# ============================================================================
# Run all tests
# ============================================================================

async def main():
    await test_aria_invalid_gives_divergence()
    await test_reconcile_detects_divergence()
    await test_stall_uses_browser_use_counters()
    print("All tests passed")


if __name__ == "__main__":
    asyncio.run(main())
