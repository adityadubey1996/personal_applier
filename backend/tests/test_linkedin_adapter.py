"""Test LinkedInAdapter — typeahead selector and is_submitted detection.

Tests the LinkedIn adapter's field selector and submission confirmation detection.
"""
import sys
import asyncio
sys.path.insert(0, str(__file__).rsplit("/tests/", 1)[0])  # add v5/backend to path

from platforms.linkedin import LinkedInAdapter, _job_ids_from_hrefs


# ============================================================================
# Test 0: deterministic job-id extraction from hrefs (the discovery core)
# ============================================================================

def test_job_ids_from_hrefs():
    hrefs = [
        "https://www.linkedin.com/jobs/view/4404480913/?refId=abc",
        "/jobs/view/1234567890/?trk=xyz",
        "https://www.linkedin.com/jobs/view/4404480913/",   # duplicate → dropped
        "https://www.linkedin.com/feed/",                    # not a job → ignored
    ]
    assert _job_ids_from_hrefs(hrefs, 10) == ["4404480913", "1234567890"]
    # n caps the result
    assert _job_ids_from_hrefs(hrefs, 1) == ["4404480913"]
    # empty / junk input is safe
    assert _job_ids_from_hrefs([], 10) == []
    assert _job_ids_from_hrefs(None, 10) == []


# ============================================================================
# Test 1: typeahead_selector contains expected strings
# ============================================================================

def test_typeahead_selector():
    """Test that typeahead_selector includes the required selectors."""
    adapter = LinkedInAdapter()
    selector = adapter.typeahead_selector

    assert "basic-typeahead__selectable" in selector, \
        f"Expected 'basic-typeahead__selectable' in selector, got: {selector}"
    assert "[role=option]" in selector, \
        f"Expected '[role=option]' in selector, got: {selector}"


# ============================================================================
# Test 2: is_submitted True on confirmation text
# ============================================================================

async def test_is_submitted_true():
    """Test is_submitted returns True when page shows 'application was sent'."""

    # Mock CDP response classes — aligned with apply_harness.py cdp_client.send.Runtime.evaluate pattern
    class FakeRuntimeEvaluate:
        async def __call__(self, params, session_id):
            return {"result": {"value": "Your application was sent. View your application"}}

    class FakeRuntime:
        evaluate = FakeRuntimeEvaluate()

    class FakeSend:
        Runtime = FakeRuntime()

    class FakeCdpClient:
        send = FakeSend()

    class FakeCdpSession:
        session_id = "fake-session-id"
        cdp_client = FakeCdpClient()

    class FakeBrowserSession:
        async def get_or_create_cdp_session(self):
            return FakeCdpSession()

    class FakeAgent:
        browser_session = FakeBrowserSession()

    adapter = LinkedInAdapter()
    result = await adapter.is_submitted(FakeAgent())
    assert result is True, f"Expected True, got {result}"


# ============================================================================
# Test 3: is_submitted False on non-confirmation text
# ============================================================================

async def test_is_submitted_false():
    """Test is_submitted returns False when page doesn't show 'application was sent'."""

    class FakeRuntimeEvaluate:
        async def __call__(self, params, session_id):
            return {"result": {"value": "Fill out the application form."}}

    class FakeRuntime:
        evaluate = FakeRuntimeEvaluate()

    class FakeSend:
        Runtime = FakeRuntime()

    class FakeCdpClient:
        send = FakeSend()

    class FakeCdpSession:
        session_id = "fake-session-id"
        cdp_client = FakeCdpClient()

    class FakeBrowserSession:
        async def get_or_create_cdp_session(self):
            return FakeCdpSession()

    class FakeAgent:
        browser_session = FakeBrowserSession()

    adapter = LinkedInAdapter()
    result = await adapter.is_submitted(FakeAgent())
    assert result is False, f"Expected False, got {result}"


# ============================================================================
# Run all tests
# ============================================================================

async def main():
    test_job_ids_from_hrefs()
    test_typeahead_selector()
    await test_is_submitted_true()
    await test_is_submitted_false()
    print("All tests passed")


if __name__ == "__main__":
    asyncio.run(main())
